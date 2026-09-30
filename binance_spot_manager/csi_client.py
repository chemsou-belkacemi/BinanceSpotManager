"""Client de l'API locale de CryptoSignalIntelligence (CSI) : lecture et évaluation seulement.

CSI est le « cerveau » : il évalue un signal (vetos déterministes, taux de base historique de la
même géométrie dans le même régime, bilan du groupe) et donne un avis. Il ne place aucun ordre et
n'a aucune clé Binance ; ce client ne fait que des lectures et des demandes d'évaluation. Une panne
de CSI se traduit par `CsiUnavailable`, jamais par une exception qui casserait l'interface ou le
worker : c'est `GatePolicy` qui décide alors si un signal automatique est retenu.

Configuration (variables d'environnement, jamais dans le code) :
- BSM_CSI_API_URL : adresse de l'API (défaut http://127.0.0.1:8503 ; http://csi-api:8503 dans Compose) ;
- CSI_API_TOKEN : jeton facultatif, le même que celui défini côté CSI.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import requests

DEFAULT_URL = "http://127.0.0.1:8503"
#: Avis qui retiennent toujours une exécution automatique quand le contrôle est actif.
HOLD_VERDICTS = frozenset({"REFUSE", "DEFAVORABLE"})
VERDICT_LABELS = {
    "REFUSE": "Refusé",
    "DEFAVORABLE": "Défavorable",
    "INDETERMINE": "Indéterminé",
    "FAVORABLE": "Favorable",
}
VERDICT_ICONS = {"REFUSE": "⛔", "DEFAVORABLE": "🔴", "INDETERMINE": "🟡", "FAVORABLE": "🟢"}
#: L'API refuse un corps de plus de 16 Ko : le texte est tronqué bien avant.
MAX_TEXT_CHARS = 12_000
MAX_SOURCE_CHARS = 80

#: Clés des réglages (data/settings.json, Settings → Signaux).
GATE_ENABLED_KEY = "signal_csi_gate_enabled"
GATE_HOLD_INDETERMINE_KEY = "signal_csi_hold_indetermine"
GATE_WHEN_UNAVAILABLE_KEY = "signal_csi_when_unavailable"  # HOLD | ALLOW
SOURCE_NAMES_KEY = "signal_csi_source_names"


class CsiUnavailable(RuntimeError):
    """CSI injoignable, en erreur ou réponse inattendue. Le message ne contient jamais le jeton."""


def verdict_label(verdict: str) -> str:
    return VERDICT_LABELS.get(str(verdict), str(verdict) or "inconnu")


@dataclass(frozen=True)
class CsiOpinion:
    verdict: str
    summary: str
    source: str
    evaluated_at: str
    record_id: str | None = None
    failed_checks: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return verdict_label(self.verdict)

    @property
    def icon(self) -> str:
        return VERDICT_ICONS.get(self.verdict, "")

    @property
    def holds_execution(self) -> bool:
        return self.verdict in HOLD_VERDICTS


class CsiClient:
    def __init__(self, base_url: str = DEFAULT_URL, token: str = "", *, timeout: float = 8.0, session=None):
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._session = session

    @classmethod
    def from_env(cls, environ=None) -> CsiClient:
        environ = os.environ if environ is None else environ
        return cls(environ.get("BSM_CSI_API_URL") or DEFAULT_URL, environ.get("CSI_API_TOKEN") or "")

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _request(self, method: str, path: str, *, json=None, params=None) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.session.request(
                method, self.base_url + path, json=json, params=params, headers=headers, timeout=self.timeout,
            )
        except (requests.RequestException, OSError) as exc:
            # Pas le message brut : il peut contenir l'URL complète ; jamais le jeton.
            raise CsiUnavailable(f"CSI injoignable sur {self.base_url} ({exc.__class__.__name__})") from None
        try:
            payload = response.json()
        except ValueError:
            raise CsiUnavailable(f"réponse non JSON de CSI (HTTP {response.status_code})") from None
        if response.status_code != 200:
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise CsiUnavailable(f"CSI a répondu HTTP {response.status_code} : {detail or 'erreur'}")
        if not isinstance(payload, dict):
            raise CsiUnavailable("réponse inattendue de CSI")
        return payload

    # --- lecture -----------------------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health")

    def probe(self) -> tuple[dict | None, str]:
        """(état, '') ou (None, raison) : ne lève jamais, pour les pages Streamlit."""
        try:
            return self.health(), ""
        except CsiUnavailable as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - l'interface ne doit jamais casser à cause de CSI
            return None, f"erreur inattendue ({exc.__class__.__name__})"

    def strategies(self) -> list[dict]:
        return list(self._request("GET", "/strategies").get("strategies") or [])

    def sources(self) -> dict:
        return self._request("GET", "/sources")

    def recent(self, limit: int = 20) -> list[dict]:
        return list(self._request("GET", "/signals/recent", params={"limit": int(limit)}).get("signals") or [])

    # --- évaluation --------------------------------------------------------------------------
    def evaluate(self, text: str, *, source: str, record: bool = True) -> CsiOpinion:
        text = (text or "").strip()
        source = (source or "").strip()[:MAX_SOURCE_CHARS]
        if not text or not source:
            raise ValueError("Texte du signal et nom du groupe requis.")
        payload = self._request(
            "POST", "/evaluate", json={"text": text[:MAX_TEXT_CHARS], "source": source, "record": bool(record)},
        )
        verdict = str(payload.get("verdict") or "")
        if verdict not in VERDICT_LABELS:
            raise CsiUnavailable(f"verdict CSI inconnu : {verdict!r}")
        failed = tuple(
            f"{check.get('label')} : {check.get('detail')}"
            for check in payload.get("checks") or [] if not check.get("ok")
        )
        return CsiOpinion(
            verdict=verdict, summary=str(payload.get("summary_fr") or ""), source=source,
            evaluated_at=str(payload.get("evaluated_at") or ""), record_id=payload.get("record_id"),
            failed_checks=failed, raw=payload,
        )


@dataclass(frozen=True)
class GatePolicy:
    """Ce que l'avis CSI fait d'un signal Telegram AUTOMATIQUE : il peut le retenir, jamais l'envoyer."""

    enabled: bool = True
    hold_indetermine: bool = False
    allow_when_unavailable: bool = False

    @classmethod
    def from_mapping(cls, preferences) -> GatePolicy:
        preferences = preferences if isinstance(preferences, dict) else {}
        return cls(
            enabled=bool(preferences.get(GATE_ENABLED_KEY, True)),
            hold_indetermine=bool(preferences.get(GATE_HOLD_INDETERMINE_KEY, False)),
            allow_when_unavailable=str(preferences.get(GATE_WHEN_UNAVAILABLE_KEY, "HOLD")).upper() == "ALLOW",
        )

    def decide(self, opinion: CsiOpinion | None, *, failure: str = "") -> tuple[bool, str]:
        """(exécution automatique permise, détail). `opinion` None = CSI injoignable (`failure`)."""
        if not self.enabled:
            return True, "avis CSI non demandé (contrôle désactivé dans Settings → Signaux)"
        if opinion is None:
            if self.allow_when_unavailable:
                return True, f"avis CSI indisponible ({failure}) : exécution permise par le réglage"
            return False, (f"Avis CSI indisponible ({failure}) : exécution automatique retenue, "
                           "confirmation manuelle possible sur la page Signaux")
        held = opinion.holds_execution or (self.hold_indetermine and opinion.verdict == "INDETERMINE")
        if held:
            return False, (f"Avis CSI {opinion.label} : {opinion.summary} — exécution automatique retenue, "
                           "confirmation manuelle possible sur la page Signaux")
        return True, f"Avis CSI {opinion.label} : {opinion.summary}"


def source_names(text) -> dict[str, str]:
    """Réglage « identifiant=nom » (une paire par ligne, virgule ou point-virgule) → {identifiant: nom}."""
    names = {}
    for part in re.split(r"[\n,;]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Nom de groupe invalide : « {part} » (attendu : identifiant=nom).")
        chat, name = (piece.strip() for piece in part.split("=", 1))
        if not re.fullmatch(r"-?\d+", chat) or not name:
            raise ValueError(f"Nom de groupe invalide : « {part} » (attendu : identifiant numérique=nom).")
        names[chat] = name[:MAX_SOURCE_CHARS]
    return names


def source_label(row, preferences=None) -> str:
    """Nom du groupe transmis à CSI pour son bilan par source.

    Signal Telegram : nom déclaré dans les réglages pour l'identifiant du chat, sinon « telegram <id> ».
    Signal collé à la main : « manuel ».
    """
    if not isinstance(row, dict) or row.get("source") != "telegram":
        return "manuel"
    parts = str(row.get("external_id") or "").split(":")
    chat = parts[1] if len(parts) >= 3 else ""
    try:
        names = source_names((preferences or {}).get(SOURCE_NAMES_KEY, "")) if isinstance(preferences, dict) else {}
    except ValueError:
        names = {}
    return names.get(chat) or (f"telegram {chat}" if chat else "telegram")
