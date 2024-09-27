import os
import time
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Reconcile")


# Load Kubernetes config
def load_kube_config():
    try:
        if os.getenv("KUBECONFIG"):
            config.load_kube_config(config_file=os.getenv("KUBECONFIG"))
        else:
            config.load_kube_config()
    except Exception as e:
        logger.error(f"Error loading Kubernetes configuration: {e}")
        exit(1)


def annotate_flux_crd(group, version, namespace, plural, name):
    """Annotate a Flux custom resource to trigger reconciliation."""
    timestamp = str(int(time.time()))  # Use the current Unix timestamp
    annotation = {
        "metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": timestamp}}
    }

    try:
        # Patch the custom object with the annotation
        client.CustomObjectsApi().patch_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=plural,
            name=name,
            body=annotation,
            field_manager="flux-client-side-apply",
        )
        logger.info(
            f"Annotated {plural}/{name} in {namespace} with reconcile timestamp {timestamp}"
        )
    except ApiException as e:
        logger.error(f"Error annotating {plural}/{name}: {e}")


def reconcile_all_flux_resources():
    """Scan all Flux-related CRDs and annotate them to trigger reconciliation."""
    try:
        crd_api = client.CustomObjectsApi()

        # Define all Flux CRD groups, versions, and plurals
        flux_crds = [
            {
                "group": "source.toolkit.fluxcd.io",
                "version": "v1beta1",
                "plural": "gitrepositories",
            },
            {
                "group": "source.toolkit.fluxcd.io",
                "version": "v1beta1",
                "plural": "helmrepositories",
            },
            {
                "group": "source.toolkit.fluxcd.io",
                "version": "v1beta1",
                "plural": "buckets",
            },
            {
                "group": "kustomize.toolkit.fluxcd.io",
                "version": "v1beta1",
                "plural": "kustomizations",
            },
            {
                "group": "helm.toolkit.fluxcd.io",
                "version": "v2beta1",
                "plural": "helmreleases",
            },
        ]

        # Scan each CRD and annotate all objects in each namespace
        for crd in flux_crds:
            group = crd["group"]
            version = crd["version"]
            plural = crd["plural"]

            # List all namespaces
            v1 = client.CoreV1Api()
            namespaces = v1.list_namespace().items
            for ns in namespaces:
                namespace = ns.metadata.name
                try:
                    resources = crd_api.list_namespaced_custom_object(
                        group=group, version=version, namespace=namespace, plural=plural
                    )
                    for resource in resources.get("items", []):
                        name = resource["metadata"]["name"]
                        annotate_flux_crd(group, version, namespace, plural, name)
                except ApiException as e:
                    if e.status != 404:
                        logger.error(f"Error listing {plural} in {namespace}: {e}")

    except Exception as e:
        logger.error(f"Error during reconciliation: {e}")


if __name__ == "__main__":
    load_kube_config()
    reconcile_all_flux_resources()
