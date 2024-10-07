import os
import base64
import subprocess
import tempfile
import shutil
import logging
import traceback
import sys

import yaml
from github import Github, GithubException
from kubernetes import client
from kubernetes.client.rest import ApiException
from subprocess import CalledProcessError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluxified.k8s.with_context import FluxContext

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Bootstrap")

# Kubernetes config and clients
NAMESPACE = "flux-system"
SECRET_NAME = "flux-system-ssh"
BOOTSTRAP_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "../../bootstrap")
)
FLUX_CRD_URL = "https://github.com/fluxcd/flux2/releases/latest/download/install.yaml"


def get_github_repo_url() -> str:
    """Get the default GitHub repository in 'owner/repo' format."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        repo_url = result.stdout.decode("utf-8").strip()
        if repo_url.startswith("git@"):
            repo_path = repo_url.split(":", 1)[1]
        elif repo_url.startswith("https://"):
            repo_path = repo_url.split("github.com/", 1)[1]
        else:
            logger.error("Unsupported GitHub repository URL format.")
            return None
        return repo_path.rstrip(".git")
    except CalledProcessError as e:
        logger.error(f"Error retrieving GitHub repository URL: {e}")
        return None


def get_github_known_hosts() -> str:
    """Fetch known hosts entries for GitHub using ssh-keyscan."""
    try:
        result = subprocess.run(
            ["ssh-keyscan", "github.com"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.decode("utf-8").strip()
    except CalledProcessError as e:
        logger.error(f"Error fetching known hosts from GitHub: {e}")
        return None


def check_github_deploy_key(g: Github, GITHUB_REPO: str, DEPLOY_KEY_NAME: str) -> bool:
    """Check if the deploy key exists in the GitHub repository."""
    try:
        repo = g.get_repo(GITHUB_REPO)
        keys = repo.get_keys()
        for key in keys:
            if key.title == DEPLOY_KEY_NAME:
                logger.info("Deploy key already exists in GitHub.")
                return True
        return False
    except GithubException as e:
        logger.error(f"Error checking deploy key: {e}")
        return False


def add_github_deploy_key(
    g: Github, GITHUB_REPO: str, DEPLOY_KEY_NAME: str, public_key: str
):
    """Add a deploy key to the GitHub repository."""
    try:
        repo = g.get_repo(GITHUB_REPO)
        repo.create_key(title=DEPLOY_KEY_NAME, key=public_key, read_only=False)
        logger.info("Deploy key added to GitHub.")
    except GithubException as e:
        logger.error(f"Error adding deploy key: {e}")


def check_kubernetes_secret(v1) -> bool:
    """Check if the Kubernetes secret for the SSH key exists."""
    try:
        v1.read_namespaced_secret(SECRET_NAME, NAMESPACE)
        logger.info("Kubernetes secret already exists.")
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        logger.error(f"Error checking Kubernetes secret: {e}")
        return False


def create_kubernetes_namespace(v1):
    """Ensure the flux-system namespace exists."""
    try:
        v1.read_namespace(NAMESPACE)
        logger.info(f"Namespace {NAMESPACE} already exists.")
    except ApiException as e:
        if e.status == 404:
            namespace = client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
            v1.create_namespace(namespace)
            logger.info(f"Namespace {NAMESPACE} created.")
        else:
            logger.error(f"Error checking namespace: {e}")


def create_ssh_keypair() -> tuple:
    """Generate an SSH keypair for Flux in a temporary directory."""
    temp_dir = tempfile.mkdtemp()
    key_path = os.path.join(temp_dir, "flux-repo-key")
    logger.info(f"Generating SSH key pair in {temp_dir}...")

    try:
        subprocess.check_call(
            [
                "ssh-keygen",
                "-t",
                "rsa",
                "-b",
                "4096",
                "-C",
                "flux",
                "-f",
                key_path,
                "-N",
                "",
            ]
        )
    except CalledProcessError as e:
        logger.error(f"Error generating SSH key pair: {e}")
        return None, None, None

    with open(f"{key_path}.pub", "r") as f:
        public_key = f.read().strip()
    with open(key_path, "r") as f:
        private_key = f.read().strip()

    return public_key, private_key, temp_dir


def create_kubernetes_secret(v1, private_key: str, known_hosts: str):
    """Create a Kubernetes secret for the SSH key and known hosts."""
    secret_data = {
        "identity": base64.b64encode(private_key.encode()).decode(),
        "known_hosts": base64.b64encode(known_hosts.encode()).decode(),
    }
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=SECRET_NAME, namespace=NAMESPACE),
        data=secret_data,
        type="Opaque",
    )
    try:
        v1.create_namespaced_secret(NAMESPACE, secret)
        logger.info("Kubernetes secret created.")
    except ApiException as e:
        logger.error(f"Error creating Kubernetes secret: {e}")


def check_flux_crds(crd_api) -> bool:
    """Check if Flux CRDs are installed."""
    try:
        crds = crd_api.list_custom_resource_definition()
        required_crds = [
            "buckets.source.toolkit.fluxcd.io",
            "gitrepositories.source.toolkit.fluxcd.io",
            "kustomizations.kustomize.toolkit.fluxcd.io",
            "helmrepositories.source.toolkit.fluxcd.io",
            "helmreleases.helm.toolkit.fluxcd.io",
        ]

        installed_crds = {crd.metadata.name for crd in crds.items}
        missing_crds = [crd for crd in required_crds if crd not in installed_crds]

        if missing_crds:
            logger.info(f"Missing CRDs: {missing_crds}")
            return False
        logger.info("All required Flux CRDs are installed.")
        return True
    except ApiException as e:
        logger.error(f"Error checking Flux CRDs: {e}")
        return False


def install_flux_crds():
    """Install Flux CRDs from the official URL."""
    try:
        logger.info("Installing Flux CRDs...")
        subprocess.check_call(["kubectl", "apply", "-f", FLUX_CRD_URL])
        logger.info("Flux CRDs installed successfully.")
    except CalledProcessError as e:
        logger.error(f"Error installing Flux CRDs: {e}")


def apply_bootstrap_resources(v1, crd_api):
    """Apply bootstrap Kubernetes manifests."""
    try:
        if not os.path.isdir(BOOTSTRAP_PATH):
            raise FileNotFoundError(
                f"Bootstrap path {BOOTSTRAP_PATH} is not a valid directory."
            )

        # Load all YAML files in the bootstrap directory
        for root, dirs, files in os.walk(BOOTSTRAP_PATH):
            for file in files:
                if file.endswith(".yaml") or file.endswith(".yml"):
                    file_path = os.path.join(root, file)
                    with open(file_path, "r") as f:
                        resources = list(yaml.safe_load_all(f))
                        for resource in resources:
                            api_version = resource.get("apiVersion", "")
                            kind = resource.get("kind", "")
                            metadata = resource.get("metadata", {})
                            name = metadata.get("name", "")
                            namespace = metadata.get("namespace", NAMESPACE)

                            if "toolkit.fluxcd.io" in api_version:
                                # Handle Flux custom resources
                                group, version = api_version.split("/", 1)
                                plural = get_plural(kind)

                                # Delete the existing custom object if it exists
                                try:
                                    crd_api.get_namespaced_custom_object(
                                        group=group,
                                        version=version,
                                        namespace=namespace,
                                        plural=plural,
                                        name=name,
                                    )
                                    logger.info(
                                        f"Deleting existing Flux custom object {kind}/{name} in {namespace}"
                                    )
                                    crd_api.delete_namespaced_custom_object(
                                        group=group,
                                        version=version,
                                        namespace=namespace,
                                        plural=plural,
                                        name=name,
                                    )
                                except ApiException as e:
                                    if e.status != 404:
                                        logger.error(
                                            f"Error checking/deleting Flux custom object {kind}/{name}: {e}"
                                        )

                                # Create the new Flux custom object
                                logger.info(
                                    f"Creating Flux custom object {kind}/{name} in {namespace}"
                                )
                                crd_api.create_namespaced_custom_object(
                                    group=group,
                                    version=version,
                                    namespace=namespace,
                                    plural=plural,
                                    body=resource,
                                )
                            else:
                                # Handle core Kubernetes resources
                                if hasattr(v1, f"create_namespaced_{kind.lower()}"):
                                    create_fn = getattr(
                                        v1, f"create_namespaced_{kind.lower()}"
                                    )
                                    try:
                                        # Check if the resource exists and delete if it does
                                        resource_exists = getattr(
                                            v1, f"read_namespaced_{kind.lower()}"
                                        )
                                        resource_exists(namespace=namespace, name=name)
                                        logger.info(
                                            f"Deleting existing resource {kind}/{name} in {namespace}"
                                        )
                                        delete_fn = getattr(
                                            v1, f"delete_namespaced_{kind.lower()}"
                                        )
                                        delete_fn(namespace=namespace, name=name)
                                    except ApiException as e:
                                        if e.status != 404:
                                            logger.error(
                                                f"Error checking/deleting resource {kind}/{name}: {e}"
                                            )

                                    # Create the new resource
                                    logger.info(
                                        f"Creating resource {kind}/{name} in {namespace}"
                                    )
                                    create_fn(namespace=namespace, body=resource)
                                else:
                                    logger.error(f"Unsupported resource kind: {kind}")
        logger.info("Bootstrap resources applied successfully.")
    except Exception as e:
        logger.error(f"Error applying bootstrap resources: {e}", exc_info=True)
        logger.debug(traceback.format_exc())


def get_plural(kind):
    """Return the plural form of a Kubernetes resource kind."""
    # Map of known singular to plural mappings for Flux CRDs
    plural_map = {
        "Kustomization": "kustomizations",
        "GitRepository": "gitrepositories",
        "HelmRelease": "helmreleases",
        "HelmRepository": "helmrepositories",
        "Bucket": "buckets",
    }
    return plural_map.get(kind, f"{kind.lower()}s")  # Simple pluralization


def main():
    with FluxContext() as flux:
        v1 = flux.v1
        crd_api = flux.crd_api

        # Get the GitHub token from environment variables
        GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
        if not GITHUB_TOKEN:
            logger.error(
                "GitHub token not found. Please set the GITHUB_TOKEN environment variable."
            )
            return

        # Get the GitHub repository in 'owner/repo' format
        GITHUB_REPO = get_github_repo_url()
        if not GITHUB_REPO:
            logger.error("GitHub repository URL not found or unsupported format.")
            return

        DEPLOY_KEY_NAME = "flux-deploy-key"

        if not os.path.exists(BOOTSTRAP_PATH):
            raise FileNotFoundError(
                f"Failed to find bootstrap path at {BOOTSTRAP_PATH} !!"
            )

        # Initialize GitHub client
        g = Github(GITHUB_TOKEN)

        # Check and install Flux CRDs if missing
        if not check_flux_crds(crd_api):
            install_flux_crds()

        public_key_exists = check_github_deploy_key(g, GITHUB_REPO, DEPLOY_KEY_NAME)

        create_kubernetes_namespace(v1)
        secret_exists = check_kubernetes_secret(v1)

        temp_dir = None

        if not public_key_exists or not secret_exists:
            public_key, private_key, temp_dir = create_ssh_keypair()
            if not public_key or not private_key:
                logger.error("Failed to generate SSH key pair.")
                return

            known_hosts = get_github_known_hosts()
            if not known_hosts:
                logger.error(
                    "Failed to retrieve GitHub known hosts, aborting secret creation."
                )
                return

            if not public_key_exists:
                add_github_deploy_key(g, GITHUB_REPO, DEPLOY_KEY_NAME, public_key)

            if not secret_exists:
                create_kubernetes_secret(v1, private_key, known_hosts)

        apply_bootstrap_resources(v1, crd_api)

        # Clean up the temporary directory if it was created
        if temp_dir:
            shutil.rmtree(temp_dir)
            logger.info(f"Temporary directory {temp_dir} removed.")


if __name__ == "__main__":
    main()
