import logging
import os

from kubernetes import client, config


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FluxContext")


class FluxContext:
    def __enter__(self):
        """Load Kubernetes configuration and initialize APIs when entering the context."""
        try:
            if os.getenv("KUBECONFIG"):
                config.load_kube_config(config_file=os.getenv("KUBECONFIG"))
            else:
                config.load_kube_config()
            self.v1 = client.CoreV1Api()
            self.crd_api = client.CustomObjectsApi()
            logger.info("Kubernetes configuration loaded successfully.")
        except Exception as e:
            logger.error(f"Error loading Kubernetes configuration: {e}")
            exit(1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clean up resources when exiting the context (if necessary)."""
        if exc_type is not None:
            logger.error(f"An exception occurred: {exc_val}")
        logger.info("Exiting the FluxContext")
