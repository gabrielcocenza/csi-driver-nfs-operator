#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""Charm for deploying csi-driver-nfs on Charmed Kubernetes worker nodes."""

import logging
import subprocess
from pathlib import Path
from typing import Tuple, Union

import ops
import pydantic
from charmlibs import apt, snap
from charmlibs.apt import PackageError
from ops.interface_kube_control import KubeControlRequirer

logger = logging.getLogger(__name__)

HELM_RELEASE = "csi-driver-nfs"
HELM_NAMESPACE = "kube-system"
HELM_BIN = "helm"
KUBECONFIG_PATH = Path("/root/.kube/csi-driver-nfs.kubeconfig")
K8S_CA_CERT_PATH = Path("/etc/kubernetes/pki/ca.crt")
HELM_UNINSTALL_TIMEOUT = "2m"
HELM_UNINSTALL_SUBPROCESS_TIMEOUT = 130


class CharmConfig(pydantic.BaseModel):
    """Typed charm configuration validated by pydantic.

    Field names use snake_case; ``CharmBase.load_config`` translates the
    kebab-case Juju option names (``kubelet-dir``, ``deploy-external-snapshotter``)
    automatically.
    """

    kubelet_dir: str = pydantic.Field(default="/var/lib/kubelet")
    deploy_external_snapshotter: bool = pydantic.Field(default=True)

    @pydantic.field_validator("kubelet_dir")
    @classmethod
    def _validate_kubelet_dir(cls, value: str) -> str:
        """Ensure kubelet-dir is a non-empty absolute path.

        We deliberately do not check that the directory exists on disk:
        the charm runs as a subordinate and may be installed before the
        kubelet has populated the path, so a filesystem check would put
        the unit into a spurious BlockedStatus on early hooks.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("kubelet-dir must not be empty")
        if not stripped.startswith("/"):
            raise ValueError(
                f"kubelet-dir must be an absolute path (start with '/'), got {value!r}"
            )
        if "\x00" in stripped:
            raise ValueError("kubelet-dir must not contain NUL bytes")
        return stripped


class CsiDriverNfsCharm(ops.CharmBase):
    """Subordinate charm that deploys the NFS CSI driver on Charmed Kubernetes."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self.typed_config = self.load_config(CharmConfig, errors="blocked")

        self.kube_control = KubeControlRequirer(self, "kube-control", schemas="0,1")

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        framework.observe(self.on.config_changed, self._reconcile)
        framework.observe(self.on.kube_control_relation_created, self._on_kube_control_joined)
        framework.observe(self.on.kube_control_relation_joined, self._on_kube_control_joined)
        framework.observe(self.on.kube_control_relation_changed, self._reconcile)
        framework.observe(
            self.on.kube_control_relation_departed,
            self._on_relation_departed_cleanup,
        )
        framework.observe(
            self.on.juju_info_relation_departed,
            self._on_relation_departed_cleanup,
        )
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on.remove, self._on_remove)

    def _on_install(self, event: Union[ops.InstallEvent, ops.UpgradeCharmEvent]) -> None:
        """Install host-level dependencies on every unit, then reconcile."""
        self._install_nfs_common()

        self._install_helm()

        self._reconcile(event)

    def _on_kube_control_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Advertise auth request to the control-plane when the relation is joined."""
        self.kube_control.set_auth_request(self.unit.name, "system:masters")
        self._reconcile(event)

    def _reconcile(self, event: ops.EventBase) -> None:
        """Reconcile desired state — only the leader unit deploys via helm."""
        if not self.kube_control.is_ready:
            self.unit.status = ops.BlockedStatus("waiting for kube-control relation")
            return

        if not self.kube_control.get_auth_credentials(self.unit.name):
            self.unit.status = ops.WaitingStatus("waiting for kube-control credentials")
            return

        self.unit.status = ops.WaitingStatus("deploying csi-driver-nfs")
        try:
            self._write_kubeconfig()
        except Exception as e:  # noqa: BLE001
            self.unit.status = ops.WaitingStatus(f"waiting for kubeconfig: {e}")
            return
        try:
            if self.unit.is_leader():
                self._deploy_nfs_csi()
        except subprocess.CalledProcessError as e:
            logger.error("failed to deploy csi-driver-nfs: %s", e.stderr)
            raise
        self.unit.status = ops.ActiveStatus()

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Ensure the nfs kernel module is loaded and refresh status."""
        if not self.unit.is_leader():
            return

        if not self.kube_control.is_ready:
            self.unit.status = ops.BlockedStatus("waiting for kube-control relation")
            return

        if not self.kube_control.get_auth_credentials(self.unit.name):
            self.unit.status = ops.WaitingStatus("waiting for kube-control credentials")
            return

        if not KUBECONFIG_PATH.exists():
            self.unit.status = ops.WaitingStatus("kubeconfig not yet written; try reconciling")
            return

        try:
            subprocess.run(  # noqa: S603
                [
                    HELM_BIN,
                    "status",
                    HELM_RELEASE,
                    "--namespace",
                    HELM_NAMESPACE,
                    "--kubeconfig",
                    str(KUBECONFIG_PATH),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.unit.status = ops.ActiveStatus()
        except subprocess.CalledProcessError:
            self.unit.status = ops.WaitingStatus("helm release not found; try reconciling")

    def _on_relation_departed_cleanup(self, event: ops.RelationDepartedEvent) -> None:
        """Uninstall the helm release on relation-departed (kube-control or juju-info).

        Both hooks fire on subordinate teardown; whichever fires first
        does the work.

        Runs on every unit, not just the leader, because:
        * The helm release is cluster-scoped (cluster-wide Kubernetes
          resources). Any unit uninstalling removes the release globally.
        * ``helm uninstall --ignore-not-found`` makes concurrent calls
          from multiple subordinates safe: the first wins, others no-op.
        """
        if not KUBECONFIG_PATH.exists():
            logger.info("kubeconfig not found on relation-departed; nothing to uninstall")
            return

        success, stderr = self._helm_uninstall()
        if success:
            logger.info("helm uninstall succeeded")
            KUBECONFIG_PATH.unlink(missing_ok=True)
            return

        message = f"helm uninstall failed: {stderr.strip()[:200]}"
        logger.error(message)
        if self.unit.is_leader():
            self.unit.status = ops.BlockedStatus(message)

    def _on_remove(self, event: ops.RemoveEvent) -> None:
        """Remove the helm snap from every unit.

        Helm release cleanup is performed in
        :meth:`_on_relation_departed_cleanup` while the on-disk
        kubeconfig is still usable. By the time ``remove`` fires, the
        relations are broken and the auth token in the kubeconfig has
        typically been revoked by the control plane.
        """
        self._uninstall_helm()

    def _install_nfs_common(self) -> None:
        """Install the nfs-common package on the host."""
        self.unit.status = ops.MaintenanceStatus("installing nfs-common")
        try:
            apt.add_package("nfs-common")
            logger.info("installed nfs-common")
            self.unit.status = ops.ActiveStatus()
        except PackageError as e:
            logger.error("failed to install nfs-common: %s", e)
            raise

    def _install_helm(self) -> None:
        """Install helm from the snap resource if provided, otherwise via the snap store."""
        self.unit.status = ops.MaintenanceStatus("installing helm")
        try:
            resource_path = self.model.resources.fetch("helm-binary")
            if resource_path.stat().st_size > 0:
                snap.install_local(str(resource_path), classic=True, dangerous=True)
                logger.info("installed helm from local snap resource")
                self.unit.status = ops.ActiveStatus()
                return
        except Exception:  # noqa: BLE001
            logger.debug("helm-binary resource not available; falling back to snap store")

        try:
            snap.add("helm", classic=True)
            logger.info("installed helm via snap store")
            self.unit.status = ops.ActiveStatus()
        except Exception as e:  # noqa: BLE001
            logger.error("failed to install helm via snap store: %s", e)
            raise

    def _uninstall_helm(self) -> None:
        """Remove the helm snap from the host."""
        try:
            snap.remove("helm")
            logger.info("removed helm snap")
        except Exception:  # noqa: BLE001
            logger.warning("failed to remove helm snap; continuing removal")

    def _helm_uninstall(self) -> Tuple[bool, str]:
        """Run ``helm uninstall`` for the csi-driver-nfs release.

        Returns a tuple ``(success, stderr)``. ``success`` is ``True`` only
        when helm exited with ``0``; ``stderr`` is the error message from
        helm (or from the wrapping exception) suitable for logging or
        surfacing in unit status.
        """
        try:
            result = subprocess.run(  # noqa: S603
                [
                    HELM_BIN,
                    "uninstall",
                    HELM_RELEASE,
                    "--namespace",
                    HELM_NAMESPACE,
                    "--ignore-not-found",
                    "--wait",
                    "--timeout",
                    HELM_UNINSTALL_TIMEOUT,
                    "--kubeconfig",
                    str(KUBECONFIG_PATH),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=HELM_UNINSTALL_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            logger.error("helm uninstall timed out after %ss", e.timeout)
            return False, f"timed out after {e.timeout}s"
        except Exception as e:  # noqa: BLE001
            logger.exception("unexpected error during helm uninstall")
            return False, str(e)

        if result.returncode != 0:
            logger.error("helm uninstall exited %d: %s", result.returncode, result.stderr)
            return False, result.stderr or f"exit code {result.returncode}"

        return True, ""

    def _write_kubeconfig(self) -> None:
        """Write kubeconfig to the fixed path using kube-control relation data."""
        KUBECONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.kube_control.create_kubeconfig(
            K8S_CA_CERT_PATH,
            KUBECONFIG_PATH,
            "root",
            self.unit.name,
        )

    def _deploy_nfs_csi(self) -> None:
        """Run ``helm upgrade --install`` for the vendored csi-driver-nfs chart."""
        charts_dir = Path(self.charm_dir) / "src" / "upstream" / "charts"
        charts = sorted(charts_dir.glob("csi-driver-nfs-*.tgz"))
        if not charts:
            raise RuntimeError("no csi-driver-nfs chart found in upstream/charts/")

        chart = charts[-1]
        kubelet_dir = self.typed_config.kubelet_dir
        snapshotter_enabled = "true" if self.typed_config.deploy_external_snapshotter else "false"

        try:
            subprocess.run(  # noqa: S603
                [
                    HELM_BIN,
                    "upgrade",
                    "--install",
                    HELM_RELEASE,
                    str(chart),
                    "--namespace",
                    HELM_NAMESPACE,
                    "--wait",
                    "--set",
                    f"node.kubeletDir={kubelet_dir}",
                    "--set",
                    f"externalSnapshotter.enabled={snapshotter_enabled}",
                    "--kubeconfig",
                    str(KUBECONFIG_PATH),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("installed csi-driver-nfs helm release")
        except subprocess.CalledProcessError as e:
            logger.error("helm upgrade/install failed: %s", e.stderr)
            raise


if __name__ == "__main__":  # pragma: nocover
    ops.main(CsiDriverNfsCharm)
