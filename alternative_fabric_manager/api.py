# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""The subset of UFM's REST API that networking-mlnx's mlnx_sdn_assist uses

networking-mlnx sends, with "Authorization: Basic <token>":

- POST <url>/<domain>/Network, PUT and DELETE <url>/<domain>/Network/<id>
- POST <url>/<domain>/Port, PUT and DELETE <url>/<domain>/Port/<id>

with the Neutron network or port as the JSON body. A change is answered with
the path of a job, as a JSON string, which it then polls with
GET <url>/<job path> until the job's "Status" is "Completed". 400 means the
request can never succeed. A DELETE of something that isn't recorded still
gets a job, rather than 404, which networking-mlnx would take as done before
OpenSM had applied the deletion.
"""

import hmac
import logging
from typing import Annotated, Any

import fastapi
from fastapi import responses

from alternative_fabric_manager import db, partitions
from alternative_fabric_manager import manager as manager_mod

LOG = logging.getLogger(__name__)

JOB_PATH = "app/jobs/%d"
JsonBody = Annotated[dict[str, Any], fastapi.Body()]


async def _lenient_body(request: fastapi.Request):
    """A DELETE's body: networking-mlnx sends the object's dict, whose
    revision_number the manager records (see specs/ha.md, Late requests).
    Anything but a JSON object counts as no body, so that a DELETE always
    gets a job."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - empty, or not JSON
        return None
    return data if isinstance(data, dict) else None


OptionalJsonBody = Annotated[dict[str, Any] | None, fastapi.Depends(_lenient_body)]


def create_app(config, manager, coordinator=None):
    prefix = "/" + config.url_prefix.strip("/") if config.url_prefix else ""
    expected = (f"Basic {config.token}").encode()

    def authenticate(authorization: Annotated[str, fastapi.Header()] = ""):
        if not hmac.compare_digest(authorization.encode(), expected):
            raise fastapi.HTTPException(status_code=401, detail="Unauthorized")

    def check_domain(domain: str):
        if domain != config.domain:
            raise fastapi.HTTPException(
                status_code=404, detail=f"Unknown domain {domain}"
            )
        if coordinator is not None and coordinator.frozen:
            # networking-mlnx retries a 503, unlike a 400 (see Freeze).
            raise fastapi.HTTPException(
                status_code=503, detail="The fabric manager is frozen"
            )

    app = fastapi.FastAPI(title="Alternative fabric manager")
    router = fastapi.APIRouter(
        prefix=prefix, dependencies=[fastapi.Depends(authenticate)]
    )

    @app.exception_handler(partitions.InvalidRequest)
    def _invalid(_request, exc):
        LOG.warning("Rejected request: %s", exc)
        return responses.JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(db.NotServing)
    def _not_serving(_request, exc):
        # The request may succeed later, on this node or another: 503, which
        # networking-mlnx retries, never a 400 (see specs/ha.md, Galera
        # specifics)
        LOG.warning("Not serving a request: %s", exc)
        return responses.JSONResponse(status_code=503, content={"error": str(exc)})

    @app.exception_handler(manager_mod.NotFound)
    def _not_found(_request, exc):
        return responses.JSONResponse(
            status_code=404, content={"error": f"{exc} not found"}
        )

    def accepted(job_id):
        return responses.JSONResponse(status_code=202, content=JOB_PATH % job_id)

    def object_id(data, path_id=None):
        if path_id is not None:
            return path_id
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise partitions.InvalidRequest("The body has no id")
        return data["id"]

    @router.post("/{domain}/Network", status_code=202)
    def create_network(domain: str, data: JsonBody):
        check_domain(domain)
        return accepted(manager.put_network(object_id(data), data))

    @router.put("/{domain}/Network/{network_id}", status_code=202)
    def update_network(domain: str, network_id: str, data: JsonBody):
        check_domain(domain)
        return accepted(manager.put_network(network_id, data))

    @router.delete("/{domain}/Network/{network_id}", status_code=202)
    def delete_network(domain: str, network_id: str, data: OptionalJsonBody):
        check_domain(domain)
        return accepted(manager.delete_network(network_id, data))

    @router.post("/{domain}/Port", status_code=202)
    def create_port(domain: str, data: JsonBody):
        check_domain(domain)
        return accepted(manager.put_port(object_id(data), data))

    @router.put("/{domain}/Port/{port_id}", status_code=202)
    def update_port(domain: str, port_id: str, data: JsonBody):
        check_domain(domain)
        return accepted(manager.put_port(port_id, data))

    @router.delete("/{domain}/Port/{port_id}", status_code=202)
    def delete_port(domain: str, port_id: str, data: OptionalJsonBody):
        check_domain(domain)
        return accepted(manager.delete_port(port_id, data))

    @router.get("/app/jobs/{job_id}")
    def get_job(job_id: int):
        job = manager.get_job(job_id)
        body = {
            "ID": job.id,
            "Status": job.status.value,
            "Operation": job.operation,
            "Object": job.object_id,
        }
        if job.error:
            body["Error"] = job.error
        return body

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        if coordinator is not None:
            # Shutting down, or cut off from Galera: HAProxy stops sending
            # requests here, and answers 503 itself if no instance is healthy.
            ok, why = coordinator.healthy()
            if not ok:
                raise fastapi.HTTPException(status_code=503, detail=why)
        return {"status": "ok"}

    app.include_router(router)
    return app
