"""Notifications systeme du navigateur, construites sans HTML non fiable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class BrowserAlertPreferences:
    sound: bool = False
    mode: str = "auto"
    duration_seconds: int = 5

    @property
    def manual(self) -> bool:
        return self.mode == "manual"


def browser_alert_preferences(values: Mapping[str, Any]) -> BrowserAlertPreferences:
    """Lit les preferences locales, avec bornes et valeurs de secours sures."""
    mode = values.get("browser_alert_mode", "auto")
    if mode not in {"auto", "manual"}:
        mode = "auto"
    try:
        duration = int(values.get("browser_alert_duration", 5))
    except (TypeError, ValueError):
        duration = 5
    return BrowserAlertPreferences(
        sound=values.get("browser_alert_sound") is True,
        mode=mode,
        duration_seconds=max(1, min(duration, 120)),
    )


PERMISSION_HTML = """
<div id="bsm-native-notifications">
  <button type="button" id="bsm-native-enable">Activer les notifications du navigateur</button>
  <small id="bsm-native-status">Autorisation requise pour les alertes hors de cet onglet.</small>
</div>
<script>
(() => {
  const button = document.getElementById("bsm-native-enable");
  const status = document.getElementById("bsm-native-status");
  if (!button || !status) return;
  if (!("Notification" in window) || !window.isSecureContext) {
    button.hidden = true;
    status.textContent = "Notifications indisponibles : utilise localhost ou HTTPS.";
    return;
  }
  const refresh = () => {
    const permission = Notification.permission;
    button.hidden = permission !== "default";
    status.textContent = permission === "granted"
      ? "Notifications du navigateur autorisées. Garde cet onglet ouvert."
      : permission === "denied"
        ? "Notifications bloquées : change l'autorisation dans le navigateur."
        : "Clique pour autoriser les notifications du navigateur.";
  };
  button.addEventListener("click", async () => {
    try {
      await Notification.requestPermission();
    } catch (error) {
      status.textContent = "Demande d'autorisation impossible dans ce navigateur.";
    }
    refresh();
  });
  refresh();
})();
</script>
"""


def notification_html(
    record: Mapping[str, Any], *, duration_seconds: int, manual: bool,
) -> str:
    """Script isole : n'envoie rien sans permission et evite les doublons."""
    serialized = json.dumps(record, sort_keys=True, ensure_ascii=True, default=str)
    event_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
    event = str(record.get("event") or "Alerte")
    payload = {
        "title": f"Binance Demo · {record.get('symbol') or 'Bot'}",
        "body": str(record.get("message") or event),
        "tag": f"bsm-{event_id}",
        "manual": bool(manual),
        "duration_ms": max(1, min(int(duration_seconds), 120)) * 1000,
    }
    # Le texte du journal n'est jamais interpole comme du code ou du HTML.
    safe_json = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
    return f"""
<script>
(() => {{
  const alert = {safe_json};
  if (!("Notification" in window) || !window.isSecureContext ||
      Notification.permission !== "granted") return;
  const ledgerKey = "bsm.native.seen.v1";
  try {{
    let seen = JSON.parse(localStorage.getItem(ledgerKey) || "[]");
    if (!Array.isArray(seen)) seen = [];
    if (seen.includes(alert.tag)) return;
    seen.push(alert.tag);
    localStorage.setItem(ledgerKey, JSON.stringify(seen.slice(-200)));
  }} catch (error) {{
    // Le navigateur peut interdire le stockage prive ; la notification reste possible.
  }}
  try {{
    const notice = new Notification(alert.title, {{
      body: alert.body,
      tag: alert.tag,
      requireInteraction: alert.manual,
    }});
    notice.onclick = () => {{ window.focus(); notice.close(); }};
    if (!alert.manual) window.setTimeout(() => notice.close(), alert.duration_ms);
  }} catch (error) {{
    console.warn("Notification navigateur impossible", error);
  }}
}})();
</script>
"""
