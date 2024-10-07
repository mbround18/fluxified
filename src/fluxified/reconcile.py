import os
import time
import sys
from kubernetes.client.rest import ApiException
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxified.k8s.with_context import FluxContext


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Reconcile")


def annotate_flux_crd(crd_api, group, version, namespace, plural, name):
    """Annotate a Flux custom resource to trigger reconciliation."""
    timestamp = str(int(time.time()))  # Use the current Unix timestamp
    annotation = {
        "metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": timestamp}}
    }

    try:
        # Patch the custom object with the annotation
        crd_api.patch_namespaced_custom_object(
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


def reconcile_all_flux_resources(flux):
    """Scan all Flux-related CRDs and annotate them to trigger reconciliation."""
    try:
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

        # List all namespaces and scan each CRD
        namespaces = flux.v1.list_namespace().items
        for ns in namespaces:
            namespace = ns.metadata.name
            for crd in flux_crds:
                group = crd["group"]
                version = crd["version"]
                plural = crd["plural"]
                try:
                    resources = flux.crd_api.list_namespaced_custom_object(
                        group=group, version=version, namespace=namespace, plural=plural
                    )
                    for resource in resources.get("items", []):
                        name = resource["metadata"]["name"]
                        annotate_flux_crd(
                            flux.crd_api, group, version, namespace, plural, name
                        )
                except ApiException as e:
                    if e.status != 404:
                        logger.error(f"Error listing {plural} in {namespace}: {e}")

    except Exception as e:
        logger.error(f"Error during reconciliation: {e}")


if __name__ == "__main__":
    with FluxContext() as flux:
        reconcile_all_flux_resources(flux)
