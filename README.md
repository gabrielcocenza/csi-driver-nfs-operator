# csi-driver-nfs-operator

Charmhub package: csi-driver-nfs-operator
More information: https://charmhub.io/csi-driver-nfs-operator

A Juju subordinate charm that deploys the
[`csi-driver-nfs`](https://github.com/kubernetes-csi/csi-driver-nfs) Helm chart on
a Canonical Kubernetes (ck8s) cluster, so that NFS shares can be consumed as
PersistentVolumes.

## Deployment

`csi-driver-nfs-operator` is a subordinate charm. It always requires the
`kube-control` relation with the `k8s` (control-plane) charm, which provides
the credentials the leader unit uses to manage the cluster-wide Helm release.

Placement on nodes is driven by the `juju-info` relation:

- Relate `juju-info` with `k8s-worker:juju-info` to deploy the subordinate on
  worker nodes.
- Additionally relate `juju-info` with `k8s:juju-info` to also deploy on
  control-plane nodes — only required when the control plane is configured to
  accept workloads (i.e. control-plane nodes are not tainted `NoSchedule`).

At least one `juju-info` relation must be present so the subordinate has a
host to run on. Every unit ensures `nfs-common` is installed on its host.

Example:

```bash
juju deploy csi-driver-nfs-operator

# Mandatory: provides Kubernetes credentials.
juju integrate csi-driver-nfs-operator:kube-control k8s:kube-control

# Place the subordinate on worker nodes.
juju integrate csi-driver-nfs-operator:juju-info k8s-worker:juju-info

# Optional: also place the subordinate on control-plane nodes.
# Only needed when the control plane accepts workloads.
juju integrate csi-driver-nfs-operator:juju-info k8s:juju-info
```

Once integrated, the leader unit deploys the vendored `csi-driver-nfs` Helm
chart into the `kube-system` namespace using credentials supplied by the
`kube-control` relation.

## Configuration

| Option                        | Default            | Description                                                                                                                                                                    |
| ----------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `kubelet-dir`                 | `/var/lib/kubelet` | Path to the kubelet directory on each node.                                                                                                                                    |
| `deploy-external-snapshotter` | `true`             | Whether to deploy the cluster-wide external snapshot-controller (and its CRDs) alongside NFS CSI. Set to `false` if another snapshot-controller already exists in the cluster. |

## Known Issues

### Dangling Helm release after the `kube-control` relation departs

When the `kube-control` relation is departed or removed, the `k8s`
(control-plane) charm revokes the kubeconfig credentials almost immediately.
This charm does attempt to uninstall the `csi-driver-nfs` Helm release during
the relation-departed hook — while the on-disk kubeconfig file still exists —
but by the time the hook runs the API server may already reject the revoked
credentials, causing the uninstall to fail.

As a consequence, removing the application (or the `kube-control` relation)
may leave the `csi-driver-nfs` Helm release, its workloads (DaemonSet,
StatefulSet, `CSIDriver` object), and any external snapshot-controller
resources installed in the `kube-system` namespace.

**Workaround.** After removing the charm or the `kube-control` relation,
verify the cluster state and manually uninstall any leftover release using a
kubeconfig that still has cluster access:

```bash
helm --namespace kube-system list
helm --namespace kube-system uninstall csi-driver-nfs
```

## Other resources

- [Contributing](CONTRIBUTING.md)
- [Juju documentation](https://documentation.ubuntu.com/juju/3.6/howto/manage-charms/)
