"""Independently deployable actuator process.

A decision service POSTs commands here. This process owns validation and
application: it validates the requested actuator state, then writes the
accepted state to BuildSim.

Supported actuators:
- damper:    numeric state from 0.0 to 1.0
- sprinkler: "on" or "off"
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import common


PORT = int(os.environ.get("PORT", "8080"))


def validate_command(cmd):
    """Validate and normalize an actuator command."""

    actuator = cmd.get("actuator")
    state = cmd.get("state")

    if not actuator:
        raise ValueError("missing actuator")

    if actuator == "damper":
        try:
            value = float(state)
        except (TypeError, ValueError):
            raise ValueError(
                'damper state must be a number between 0 and 1'
            )

        if not 0.0 <= value <= 1.0:
            raise ValueError(
                "damper state must be between 0 and 1"
            )

        return actuator, str(value)

    if actuator == "sprinkler":
        if state not in ("on", "off"):
            raise ValueError(
                'sprinkler state must be "on" or "off"'
            )

        return actuator, state

    raise ValueError(
        f"unsupported actuator: {actuator}"
    )


def actuator_id(actuator):
    """Return the BuildSim actuator ID for an actuator name."""

    if actuator == "damper":
        return common.DAMPER_ID

    if actuator == "sprinkler":
        return common.SETPOINT_ID

    raise ValueError(
        f"unsupported actuator: {actuator}"
    )


class Handler(BaseHTTPRequestHandler):

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()

        self.send_response(code)
        self.send_header(
            "Content-Type",
            "application/json",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()

        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._reply(
                200,
                {"ok": True},
            )

        return self._reply(
            404,
            {"error": "not found"},
        )

    def do_POST(self):
        if self.path != "/commands":
            return self._reply(
                404,
                {"error": "not found"},
            )

        # ---------------------------------------------------------------
        # Read request
        # ---------------------------------------------------------------

        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            cmd = json.loads(
                self.rfile.read(length) or b"{}"
            )

        except Exception:  # noqa: BLE001
            return self._reply(
                400,
                {
                    "error": (
                        'body must be JSON containing '
                        '"actuator" and "state"'
                    )
                },
            )

        # ---------------------------------------------------------------
        # Validate command
        # ---------------------------------------------------------------

        try:
            actuator, state = validate_command(cmd)

        except ValueError as exc:
            return self._reply(
                422,
                {"error": str(exc)},
            )

        # ---------------------------------------------------------------
        # Apply to BuildSim
        # ---------------------------------------------------------------

        try:
            buildsim_id = actuator_id(actuator)

            common.call(
                "PUT",
                f"/api/actuators/{buildsim_id}/state",
                {"state": state},
            )

        except Exception as exc:  # noqa: BLE001
            return self._reply(
                502,
                {
                    "error": (
                        f"BuildSim rejected update: {exc}"
                    )
                },
            )

        # ---------------------------------------------------------------
        # Success
        # ---------------------------------------------------------------

        reason = cmd.get(
            "reason",
            "",
        )

        print(
            f"applied actuator={actuator} "
            f"state={state} "
            f"reason={reason}",
            flush=True,
        )

        return self._reply(
            200,
            {
                "applied": True,
                "actuator": actuator,
                "state": state,
            },
        )


if __name__ == "__main__":
    common.wait_until_healthy()

    print(
        f"actuator listening on :{PORT}",
        flush=True,
    )

    HTTPServer(
        ("0.0.0.0", PORT),
        Handler,
    ).serve_forever()