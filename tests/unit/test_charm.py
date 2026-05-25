# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.
#
# To learn more about testing, see https://documentation.ubuntu.com/ops/latest/explanation/testing/

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from charmlibs.apt import PackageError
from charmlibs.snap import SnapError
from ops import testing

from charm import HELM_NAMESPACE, HELM_RELEASE, KUBECONFIG_PATH, CsiDriverNfsCharm

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_charm_root(tmp_path: Path) -> Path:
    """Create a minimal charm root with a fake vendored chart."""
    charts_dir = tmp_path / "src" / "upstream" / "charts"
    charts_dir.mkdir(parents=True)
    (charts_dir / "csi-driver-nfs-4.13.2.tgz").write_bytes(b"fake chart")
    return tmp_path


def _make_kube_control_mock(
    is_ready: bool = True,
    has_creds: bool = True,
) -> MagicMock:
    """Return a MagicMock that replaces a KubeControlRequirer instance."""
    mock = MagicMock()
    mock.is_ready = is_ready
    if has_creds and is_ready:
        mock.get_auth_credentials.return_value = {
            "user": "csi-driver-nfs-operator/0",
            "client_token": "s3cr3t-token",
            "kubelet_token": "kubelet-tok",
            "proxy_token": "proxy-tok",
        }
    else:
        mock.get_auth_credentials.return_value = None
    return mock


@contextmanager
def _patch_kube_control(mock: MagicMock):
    """Patch KubeControlRequirer so the charm's __init__ uses our mock."""
    with patch("charm.KubeControlRequirer", return_value=mock):
        yield mock


# ---------------------------------------------------------------------------
# install / upgrade-charm
# ---------------------------------------------------------------------------


def test_install_installs_nfs_common_and_helm():
    """On install, nfs-common is installed via apt and helm via snap store."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.apt.add_package") as mock_apt,
        patch("charm.snap.add") as mock_snap_add,
        patch("charm.snap.install_local") as mock_snap_local,
        patch("charm.CsiDriverNfsCharm.model", new_callable=MagicMock) as mock_model,
    ):
        mock_model.resources.fetch.side_effect = Exception("no resource")
        ctx.run(ctx.on.install(), state_in)

    mock_apt.assert_called_once_with("nfs-common")
    mock_snap_add.assert_called_once_with("helm", classic=True)
    mock_snap_local.assert_not_called()


def test_install_uses_local_snap_resource_when_provided(tmp_path):
    """On install, a non-empty helm-binary resource is installed via snap install_local."""
    fake_snap = tmp_path / "helm.snap"
    fake_snap.write_bytes(b"fake snap content")

    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.apt.add_package"),
        patch("charm.snap.add") as mock_snap_add,
        patch("charm.snap.install_local") as mock_snap_local,
        patch("charm.CsiDriverNfsCharm.model", new_callable=MagicMock) as mock_model,
    ):
        mock_model.resources.fetch.return_value = fake_snap
        ctx.run(ctx.on.install(), state_in)

    mock_snap_local.assert_called_once_with(str(fake_snap), classic=True, dangerous=True)
    mock_snap_add.assert_not_called()


# ---------------------------------------------------------------------------
# kube-control relation joined — set_auth_request
# ---------------------------------------------------------------------------


def test_kube_control_joined_sets_auth_request():
    """When kube-control is joined, set_auth_request is called with the unit name."""
    ctx = testing.Context(CsiDriverNfsCharm)
    kc_mock = _make_kube_control_mock(is_ready=False)
    relation = testing.Relation("kube-control")
    state_in = testing.State(leader=True, relations={relation})

    with _patch_kube_control(kc_mock):
        ctx.run(ctx.on.relation_joined(relation), state_in)

    kc_mock.set_auth_request.assert_called_once()
    call_args = kc_mock.set_auth_request.call_args
    assert call_args[0][1] == "system:masters"


# ---------------------------------------------------------------------------
# _reconcile — non-leader path
# ---------------------------------------------------------------------------


def test_reconcile_non_leader_sets_active():
    """Non-leader units skip reconciliation; no status is explicitly set."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.subprocess.run"),
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.unit_status == testing.UnknownStatus()


# ---------------------------------------------------------------------------
# _reconcile — leader paths
# ---------------------------------------------------------------------------


def test_reconcile_leader_no_kube_control_relation_blocked():
    """Leader with kube-control not ready gets BlockedStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock(is_ready=False, has_creds=False)

    with _patch_kube_control(kc_mock):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.unit_status == testing.BlockedStatus("waiting for kube-control relation")


def test_reconcile_leader_waiting_for_credentials():
    """Leader with kube-control ready but no credentials yet gets WaitingStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock(is_ready=True, has_creds=False)

    with _patch_kube_control(kc_mock):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.unit_status == testing.WaitingStatus("waiting for kube-control credentials")


def test_reconcile_leader_ready_deploys_helm_and_active(tmp_path):
    """Leader with full kube-control data runs helm and reaches ActiveStatus."""
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=_make_charm_root(tmp_path))
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_kc_path,
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_kc_path.__str__ = lambda _: "/root/.kube/csi-driver-nfs.kubeconfig"
        mock_run.return_value = MagicMock(returncode=0)
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.unit_status == testing.ActiveStatus()
    helm_call_args = mock_run.call_args_list[0][0][0]
    assert helm_call_args[0] == "helm"
    assert "upgrade" in helm_call_args
    assert "--install" in helm_call_args
    assert HELM_RELEASE in helm_call_args
    assert "--namespace" in helm_call_args
    assert HELM_NAMESPACE in helm_call_args
    assert "--wait" in helm_call_args


def test_reconcile_leader_helm_uses_kubelet_dir_from_config(tmp_path):
    """The kubelet-dir config option is passed to helm as node.kubeletDir."""
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=_make_charm_root(tmp_path))
    custom_dir = "/var/snap/microk8s/common/var/lib/kubelet"
    state_in = testing.State(leader=True, config={"kubelet-dir": custom_dir})
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH"),
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        ctx.run(ctx.on.config_changed(), state_in)

    helm_call_args = mock_run.call_args_list[0][0][0]
    assert f"node.kubeletDir={custom_dir}" in helm_call_args


def test_reconcile_leader_kubeconfig_write_failure_waiting(tmp_path):
    """If _write_kubeconfig raises, the unit gets WaitingStatus and helm is not called."""
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=_make_charm_root(tmp_path))
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()
    kc_mock.create_kubeconfig.side_effect = FileNotFoundError("No CA certificate found")

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH"),
        patch("charm.subprocess.run") as mock_run,
    ):
        state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.unit_status.name == "waiting"
    assert "kubeconfig" in state_out.unit_status.message
    mock_run.assert_not_called()


def test_reconcile_leader_helm_failure_raises(tmp_path):
    """If helm exits non-zero, CalledProcessError propagates (charm enters error state)."""
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=_make_charm_root(tmp_path))
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH"),
        patch("charm.subprocess.run") as mock_run,
        pytest.raises(testing.errors.UncaughtCharmError),
    ):
        mock_run.side_effect = subprocess.CalledProcessError(1, "helm")
        ctx.run(ctx.on.config_changed(), state_in)


def test_reconcile_leader_helm_failure_logs_stderr(tmp_path):
    """CalledProcessError from helm is logged before being re-raised."""
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=_make_charm_root(tmp_path))
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    err = subprocess.CalledProcessError(1, "helm", stderr="connection refused")
    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH"),
        patch("charm.subprocess.run", side_effect=err),
        patch("charm.logger") as mock_logger,
        pytest.raises(testing.errors.UncaughtCharmError),
    ):
        ctx.run(ctx.on.config_changed(), state_in)

    mock_logger.error.assert_any_call("failed to deploy csi-driver-nfs: %s", "connection refused")


# ---------------------------------------------------------------------------
# _write_kubeconfig
# ---------------------------------------------------------------------------


def test_write_kubeconfig_calls_create_kubeconfig(tmp_path):
    """_write_kubeconfig calls kube_control.create_kubeconfig with the fixed path."""
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=_make_charm_root(tmp_path))
    kc_mock = _make_kube_control_mock()
    captured = {}

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.subprocess.run"),
    ):
        with ctx(ctx.on.config_changed(), testing.State(leader=True)) as mgr:
            captured["unit_name"] = mgr.charm.unit.name

    # create_kubeconfig is called by _reconcile → _write_kubeconfig; verify the args
    kc_mock.create_kubeconfig.assert_called_with(
        mock_path, mock_path, "root", captured["unit_name"]
    )


# ---------------------------------------------------------------------------
# update-status
# ---------------------------------------------------------------------------


def test_update_status_non_leader_is_noop():
    """Non-leader update-status returns immediately without touching helm."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.subprocess.run") as mock_run,
    ):
        ctx.run(ctx.on.update_status(), state_in)

    mock_run.assert_not_called()


def test_update_status_leader_no_kube_control_blocked():
    """Leader update-status with kube-control not ready sets BlockedStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock(is_ready=False, has_creds=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.subprocess.run"),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.BlockedStatus("waiting for kube-control relation")


def test_update_status_leader_no_credentials_waiting():
    """Leader update-status with no credentials sets WaitingStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock(is_ready=True, has_creds=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.subprocess.run"),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.WaitingStatus("waiting for kube-control credentials")


def test_update_status_leader_no_kubeconfig_waiting():
    """Leader update-status without kubeconfig on disk gets WaitingStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_path.exists.return_value = False
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.WaitingStatus(
        "kubeconfig not yet written; try reconciling"
    )
    mock_run.assert_not_called()


def test_update_status_leader_helm_status_ok():
    """Leader with successful helm status gets ActiveStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_path.exists.return_value = True
        mock_run.return_value = MagicMock(returncode=0)
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.ActiveStatus()


def test_update_status_leader_helm_status_not_found():
    """Leader with failing helm status gets WaitingStatus."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_path.exists.return_value = True
        mock_run.side_effect = subprocess.CalledProcessError(1, "helm")
        state_out = ctx.run(ctx.on.update_status(), state_in)

    assert state_out.unit_status == testing.WaitingStatus(
        "helm release not found; try reconciling"
    )


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_non_leader_is_noop():
    """Non-leader remove removes the helm snap but skips the helm release uninstall."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.snap.remove") as mock_snap_remove,
        patch("charm.subprocess.run") as mock_run,
    ):
        ctx.run(ctx.on.remove(), state_in)

    mock_snap_remove.assert_called_once_with("helm")
    mock_run.assert_not_called()


def test_remove_leader_no_kube_control_skips_helm_uninstall():
    """Leader remove with kube-control not ready skips helm release uninstall but still removes snap."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.snap.remove") as mock_snap_remove,
        patch("charm.subprocess.run") as mock_run,
    ):
        ctx.run(ctx.on.remove(), state_in)

    mock_snap_remove.assert_called_once_with("helm")
    mock_run.assert_not_called()


def test_remove_leader_no_credentials_skips_helm_uninstall():
    """Leader remove with no credentials skips helm release uninstall but still removes snap."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock(is_ready=True, has_creds=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.snap.remove") as mock_snap_remove,
        patch("charm.subprocess.run") as mock_run,
        patch("charm.logger") as mock_logger,
    ):
        ctx.run(ctx.on.remove(), state_in)

    mock_snap_remove.assert_called_once_with("helm")
    mock_run.assert_not_called()
    mock_logger.warning.assert_any_call("no credentials on remove; skipping helm uninstall")


def test_remove_leader_no_kubeconfig_skips_helm_uninstall():
    """Leader remove without kubeconfig on disk skips helm release uninstall but removes snap."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.snap.remove") as mock_snap_remove,
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_path.exists.return_value = False
        ctx.run(ctx.on.remove(), state_in)

    mock_snap_remove.assert_called_once_with("helm")
    mock_run.assert_not_called()


def test_remove_leader_calls_helm_uninstall():
    """Leader remove calls helm uninstall, cleans up kubeconfig, and removes helm snap."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.snap.remove") as mock_snap_remove,
        patch("charm.subprocess.run") as mock_run,
    ):
        mock_path.exists.return_value = True
        mock_run.return_value = MagicMock(returncode=0)
        ctx.run(ctx.on.remove(), state_in)

    mock_snap_remove.assert_called_once_with("helm")
    helm_call_args = mock_run.call_args_list[0][0][0]
    assert helm_call_args[0] == "helm"
    assert "uninstall" in helm_call_args
    assert HELM_RELEASE in helm_call_args
    assert "--namespace" in helm_call_args
    assert HELM_NAMESPACE in helm_call_args
    mock_path.unlink.assert_called_once_with(missing_ok=True)


def test_remove_leader_helm_uninstall_nonzero_logs_warning():
    """Non-zero helm uninstall exit code is logged as a warning."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    failed_result = MagicMock(returncode=1, stderr="release not found")
    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.snap.remove"),
        patch("charm.subprocess.run", return_value=failed_result),
        patch("charm.logger") as mock_logger,
    ):
        mock_path.exists.return_value = True
        ctx.run(ctx.on.remove(), state_in)

    mock_logger.warning.assert_any_call(
        "helm uninstall exited %d: %s", 1, "release not found"
    )


def test_remove_leader_helm_uninstall_exception_is_swallowed():
    """An unexpected exception during helm uninstall is caught and logged."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH") as mock_path,
        patch("charm.snap.remove"),
        patch("charm.subprocess.run", side_effect=Exception("socket error")),
        patch("charm.logger") as mock_logger,
    ):
        mock_path.exists.return_value = True
        ctx.run(ctx.on.remove(), state_in)

    mock_logger.exception.assert_any_call("unexpected error during helm uninstall on remove")


# ---------------------------------------------------------------------------
# _install_nfs_common — PackageError path
# ---------------------------------------------------------------------------


def test_install_nfs_common_package_error_raises():
    """PackageError from apt.add_package propagates out of _install_nfs_common."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.apt.add_package", side_effect=PackageError("nfs-common")),
        patch("charm.snap.add"),
        patch("charm.CsiDriverNfsCharm.model", new_callable=MagicMock) as mock_model,
        pytest.raises(testing.errors.UncaughtCharmError),
    ):
        mock_model.resources.fetch.side_effect = Exception("no resource")
        ctx.run(ctx.on.install(), state_in)


# ---------------------------------------------------------------------------
# _install_helm — zero-size resource and snap.add failure
# ---------------------------------------------------------------------------


def test_install_helm_zero_size_resource_falls_back_to_snap(tmp_path):
    """A zero-byte helm-binary resource falls through to snap.add."""
    fake_snap = tmp_path / "helm.snap"
    fake_snap.write_bytes(b"")  # zero size

    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.apt.add_package"),
        patch("charm.snap.add") as mock_snap_add,
        patch("charm.snap.install_local") as mock_snap_local,
        patch("charm.CsiDriverNfsCharm.model", new_callable=MagicMock) as mock_model,
    ):
        mock_model.resources.fetch.return_value = fake_snap
        ctx.run(ctx.on.install(), state_in)

    mock_snap_local.assert_not_called()
    mock_snap_add.assert_called_once_with("helm", classic=True)


def test_install_helm_snap_add_fails_raises():
    """SnapError from snap.add propagates out of _install_helm."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock(is_ready=False)

    with (
        _patch_kube_control(kc_mock),
        patch("charm.apt.add_package"),
        patch("charm.snap.add", side_effect=SnapError("snap store unreachable")),
        patch("charm.CsiDriverNfsCharm.model", new_callable=MagicMock) as mock_model,
        pytest.raises(testing.errors.UncaughtCharmError),
    ):
        mock_model.resources.fetch.side_effect = Exception("no resource")
        ctx.run(ctx.on.install(), state_in)


# ---------------------------------------------------------------------------
# _uninstall_helm — exception swallowed
# ---------------------------------------------------------------------------


def test_uninstall_helm_exception_is_swallowed():
    """An exception from snap.remove is swallowed so removal continues."""
    ctx = testing.Context(CsiDriverNfsCharm)
    state_in = testing.State(leader=False)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.snap.remove", side_effect=SnapError("snapd not running")),
    ):
        # Should not raise
        ctx.run(ctx.on.remove(), state_in)


# ---------------------------------------------------------------------------
# _deploy_nfs_csi — missing chart
# ---------------------------------------------------------------------------


def test_deploy_nfs_csi_no_chart_raises(tmp_path):
    """RuntimeError is raised when no chart tgz is found in the charts directory."""
    # Create charm root WITHOUT any chart file
    charts_dir = tmp_path / "src" / "upstream" / "charts"
    charts_dir.mkdir(parents=True)
    ctx = testing.Context(CsiDriverNfsCharm, charm_root=tmp_path)
    state_in = testing.State(leader=True)
    kc_mock = _make_kube_control_mock()

    with (
        _patch_kube_control(kc_mock),
        patch("charm.KUBECONFIG_PATH"),
        pytest.raises(testing.errors.UncaughtCharmError) as exc_info,
    ):
        ctx.run(ctx.on.config_changed(), state_in)

    assert "no csi-driver-nfs chart found" in str(exc_info.value)
