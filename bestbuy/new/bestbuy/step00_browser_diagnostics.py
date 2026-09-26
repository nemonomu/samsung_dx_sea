"""Best-effort, bounded diagnostics using DrissionPage's public network listener.

Read only transport status and failure fields, never headers, cookies or bodies.
Diagnostics must not change request, browser lifecycle or collection behavior.
"""

from urllib.parse import urlsplit

from .step00_collection_recovery import sanitized


class BrowserDiagnostics:
    def __init__(self, page, target=r"^https://www\.bestbuy\.com/gateway/graphql(?:[?#]|$)"):
        self.page = page
        self.listener = None
        self.info = {"listener_status": "unavailable", "network_events": []}
        try:
            listener = page.listen
            if listener.listening:
                self.info["listener_status"] = "already_in_use"
                return
            self.listener = listener
            listener.start(target, is_regex=True)
            self.info["listener_status"] = "started"
        except Exception as exc:
            self.info["diagnostic_error"] = type(exc).__name__

    def finish(self, failed):
        try:
            if failed and self.listener is not None and self.info["listener_status"] == "started":
                packets = self.listener.wait(count=20, timeout=0.25, fit_count=False, raise_err=False)
                if packets:
                    for packet in packets if isinstance(packets, list) else [packets]:
                        event = {"is_failed": bool(packet.is_failed)}
                        if packet.is_failed:
                            event["source"] = "Network.loadingFailed"
                            failure = packet.fail_info
                            for key in ("errorText", "blockedReason", "canceled"):
                                value = getattr(failure, key, None)
                                if isinstance(value, (str, bool)):
                                    event[key] = value
                            cors = getattr(failure, "corsErrorStatus", None)
                            if isinstance(cors, dict):
                                # failedParameter can contain a value from a header; omit it.
                                event["corsErrorStatus"] = {"corsError": cors.get("corsError", "")}
                            elif isinstance(cors, str) and cors.isalpha():
                                event["corsErrorStatus"] = cors
                        else:
                            event["status_code"] = packet.response.status
                        self.info["network_events"].append(event)
        except Exception as exc:
            self.info["diagnostic_error"] = type(exc).__name__
        finally:
            if self.listener is not None:
                try:
                    self.listener.stop()
                except Exception as exc:
                    self.info["stop_error"] = type(exc).__name__
        if failed:
            try:
                parts = urlsplit(self.page.url)
                self.info["context_url"] = f"{parts.scheme}://{parts.hostname or ''}{parts.path}"
                state = self.page.run_js(
                    "return {ready_state:document.readyState, online:navigator.onLine};", timeout=2)
                if isinstance(state, dict):
                    if isinstance(state.get("ready_state"), str):
                        self.info["ready_state"] = state["ready_state"]
                    if isinstance(state.get("online"), bool):
                        self.info["online"] = state["online"]
            except Exception as exc:
                self.info["context_error"] = type(exc).__name__
        return sanitized(self.info)
