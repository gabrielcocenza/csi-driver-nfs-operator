#!/usr/bin/env python3
# Copyright 2026 Canonical Limited
# See LICENSE file for licensing details.

"""Charm for deploying csi-driver-nfs on Charmed Kubernetes worker nodes."""

import logging
import subprocess
from pathlib import Path
from typing import Union

import ops
from charmlibs import apt, snap
from charmlibs.apt import PackageError
from ops.interface_kube_control import KubeControlRequirer

logger = logging.getLogger(__name__)

HELM_RELEASE = "csi-driver-nfs"
HELM_NAMESPACE = "kube-system"
HELM_BIN = "helm"
KUBECONFIG_PATH = Path("/root/.kube/csi-driver-nfs.kubeconfig")


class CsiDriverNfsCharm(ops.CharmBase):
    """Subordinate charm that deploys the NFS CSI driver on Charmed Kubernetes."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self.kube_control = KubeControlRequirer(self, "kube-control", schemas="0,1")

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        framework.observe(self.on.config_changed, self._reconcile)
        framework.observe(self.on.kube_control_relation_created, self._on_kube_control_joined)
        framework.observe(self.on.kube_control_relation_joined, self._on_kube_control_joined)
        framework.observe(self.on.kube_control_relation_changed, self._reconcile)
        framework.observe(self.on.update_status, self._on_update_status)
        framework.observe(self.on.remove, self._on_remove)

    # -------------------------------------------------------------------------
    # Event handlers
    # -------------------------------------------------------------------------

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
        if not self.unit.is_leader():
            return

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

    def _on_remove(self, event: ops.RemoveEvent) -> None:
        """Uninstall the helm release (leader only) and remove the helm snap (every unit)."""

        if not self.unit.is_leader():
            self._uninstall_helm()
            return

        if not self.kube_control.is_ready:
            logger.warning("kube-control not ready on remove; skipping helm uninstall")
            self._uninstall_helm()
            return

        if not self.kube_control.get_auth_credentials(self.unit.name):
            logger.warning("no credentials on remove; skipping helm uninstall")
            self._uninstall_helm()
            return

        if not KUBECONFIG_PATH.exists():
            logger.warning("kubeconfig not found on remove; skipping helm uninstall")
            self._uninstall_helm()
            return

        try:
            result = subprocess.run(  # noqa: S603
                [
                    HELM_BIN,
                    "uninstall",
                    HELM_RELEASE,
                    "--namespace",
                    HELM_NAMESPACE,
                    "--kubeconfig",
                    str(KUBECONFIG_PATH),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.warning("helm uninstall exited %d: %s", result.returncode, result.stderr)
        except Exception:  # noqa: BLE001
            logger.exception("unexpected error during helm uninstall on remove")

        KUBECONFIG_PATH.unlink(missing_ok=True)
        self._uninstall_helm()

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _install_nfs_common(self) -> None:
        """Install the nfs-common package on the host."""
        self.unit.status = ops.MaintenanceStatus("installing nfs-common")
        try:
            apt.add_package("nfs-common")
            logger.info("installed nfs-common")
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
                return
        except Exception:  # noqa: BLE001
            logger.debug("helm-binary resource not available; falling back to snap store")

        try:
            snap.add("helm", classic=True)
            logger.info("installed helm via snap store")
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

    def _write_kubeconfig(self) -> None:
        """Write kubeconfig to the fixed path using kube-control relation data."""
        KUBECONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.kube_control.create_kubeconfig(
            KUBECONFIG_PATH,
            KUBECONFIG_PATH,
            "root",
            self.unit.name,
        )

    def _deploy_nfs_csi(self) -> None:
        """Run ``helm upgrade --install`` for the vendored csi-driver-nfs chart."""
        charts = sorted((Path(self.charm_dir) / "src" / "upstream" / "charts").glob("csi-driver-nfs-*.tgz"))
        if not charts:
            raise RuntimeError("no csi-driver-nfs chart found in upstream/charts/")
        chart = charts[-1]
        kubelet_dir = str(self.config["kubelet-dir"])

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
                "--kubeconfig",
                str(KUBECONFIG_PATH),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(CsiDriverNfsCharm)
