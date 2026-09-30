"""Connections to the dev tenant for `live` tests, using ./config.yaml and the certificate it
names."""

import httpx

from pulse.adapters.graph import CertificateCredential, GraphMailbox
from pulse.config import Config


def graph_mailbox(config: Config, client: httpx.AsyncClient, address: str) -> GraphMailbox:
    graph = config.graph
    credential = CertificateCredential(graph.tenant_id, graph.client_id, graph.certificate_path)
    return GraphMailbox(client, credential.token, address, graph.max_retries)
