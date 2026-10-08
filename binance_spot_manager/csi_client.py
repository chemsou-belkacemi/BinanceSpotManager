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

import math
import os
import re
from dataclasses import dataclass, field

import requests

DEFAULT_URL = "http://127.0.0.1:8503"
#: Avis qui retiennent toujours une exécution automatique quand le contrôle est actif
#: (EN_ATTENTE : paire en cours d'ajout chez CSI, historique pas encore téléchargé).
HOLD_VERDICTS = frozenset({"REFUSE", "DEFAVORABLE", "EN_ATTENTE"})
VERDICT_LABELS = {
    "REFUSE": "Refusé",
    "DEFAVORABLE": "Défavorable",
    "INDETERMINE": "Indéterminé",
    "FAVORABLE": "Favorable",
    "EN_ATTENTE": "En attente",
}
VERDICT_ICONS = {"REFUSE": "⛔", "DEFAVORABLE": "🔴", "INDETERMINE": "🟡", "FAVORABLE": "🟢", "EN_ATTENTE": "⏳"}
#: L'API refuse un corps de plus de 16 Ko : le texte est tronqué bien avant.
MAX_TEXT_CHARS = 12_000
MAX_SOURCE_CHARS = 80
EVALUATE_TIMEOUT_SECONDS = 30.0

#: Clés des réglages (data/settings.json, Settings → Signaux → « Liens avec CSI »).
GATE_ENABLED_KEY = "signal_csi_gate_enabled"
GATE_HOLD_INDETERMINE_KEY = "signal_csi_hold_indetermine"
GATE_WHEN_UNAVAILABLE_KEY = "signal_csi_when_unavailable"  # HOLD | ALLOW
SOURCE_NAMES_KEY = "signal_csi_source_names"
#: Conseil de taille de CSI (GET /risk), affiché à côté du budget, jamais appliqué.
SIZE_ADVICE_ENABLED_KEY = "signal_csi_size_advice_enabled"
#: Retour d'exécution vers CSI (data/signal_drop/outgoing/execution_events.jsonl), hors DRY_RUN.
FEEDBACK_ENABLED_KEY = "signal_csi_feedback_enabled"
#: Revue des signaux CSI dont VOLATILITY_REGIME vaut HIGH (lu par signal_routing.RoutingPolicy).
HIGH_VOLATILITY_REVIEW_KEY = "signal_review_csi_high_volatility"

#: Tous les liens avec CSI sont DÉSACTIVÉS par défaut : c'est le propriétaire qui les active. Ces valeurs ne
#: servent que si la clé est absente de data/settings.json ; un réglage déjà enregistré garde sa valeur.
#: Le feu CSI (csi_light.py, clé csi_light_enabled) suit la même règle.
CSI_LINK_DEFAULTS = {
    GATE_ENABLED_KEY: False,
    SIZE_ADVICE_ENABLED_KEY: False,
    HIGH_VOLATILITY_REVIEW_KEY: False,
    FEEDBACK_ENABLED_KEY: False,
}
_TRUE_TEXTS = frozenset({"1", "true", "vrai", "oui", "yes", "on"})


def link_enabled(preferences, key: str) -> bool:
    """Interrupteur d'un lien avec CSI : valeur enregistrée si présente, sinon le défaut (désactivé).

    Un texte n'active que s'il dit oui (`bool("false")` vaudrait True)."""
    preferences = preferences if isinstance(preferences, dict) else {}
    value = preferences.get(key, CSI_LINK_DEFAULTS.get(key, False))
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TEXTS
    return bool(value)


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

    def _request(self, method: str, path: str, *, json=None, params=None,
                 timeout: float | tuple[float, float] | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.session.request(
                method, self.base_url + path, json=json, params=params, headers=headers,
                timeout=timeout or self.timeout,
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

    def risk(self) -> dict:
        """Conseil de risque à 24 h de CSI (`GET /risk`, shadow) : ampleur typique et taille relative par paire."""
        return self._request("GET", "/risk")

    def meteo(self, *, timeout: float | tuple[float, float] | None = None) -> dict:
        """Feu de protection du marché de CSI (`GET /meteo`) : VERT, ORANGE, ROUGE ou INCONNU, avec ses composantes et
        une explication. Outil de prudence, aucun gain démontré ; lu par le garde-fou csi_light.py. `timeout` :
        (connexion, lecture) pour `requests`, chacun séparément ; la résolution DNS n'est pas bornée."""
        return self._request("GET", "/meteo", timeout=timeout)

    def recent(self, limit: int = 20) -> list[dict]:
        return list(self._request("GET", "/signals/recent", params={"limit": int(limit)}).get("signals") or [])

    def generated(self, limit: int = 20) -> list[dict]:
        """Derniers signaux trouvés par les stratégies de CSI, avec `bsm_text` (format lu par la page Signaux)."""
        return list(self._request("GET", "/signals/generated", params={"limit": int(limit)}).get("signals") or [])

    # --- évaluation --------------------------------------------------------------------------
    def evaluate(self, text: str, *, source: str, record: bool = True, user_validated: bool = False) -> CsiOpinion:
        """`user_validated` : signal soumis à la main par le propriétaire. Sa validation ajoute une paire
        inconnue à l'univers de CSI (avis EN_ATTENTE le temps du téléchargement). Le worker passe False : cette
        évaluation n'ajoute rien. Depuis le 2026-10-06, CSI ajoute seul les paires USDT publiées par les canaux de
        confiance halal du propriétaire, reçues par son relais Telegram (identifiant de conversation), jamais ici."""
        text = (text or "").strip()
        source = (source or "").strip()[:MAX_SOURCE_CHARS]
        if not text or not source:
            raise ValueError("Texte du signal et nom du groupe requis.")
        payload = self._request(
            "POST", "/evaluate",
            json={"text": text[:MAX_TEXT_CHARS], "source": source, "record": bool(record),
                  "user_validated": bool(user_validated)},
            # Une paire nouvelle est vérifiée sur Binance pendant la requête (jusqu'à ~15 s avec les reprises).
            timeout=max(self.timeout, EVALUATE_TIMEOUT_SECONDS),
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
    """Ce que l'avis CSI fait d'un signal Telegram AUTOMATIQUE : il peut le retenir, jamais l'envoyer.

    Désactivé par défaut : sans réglage enregistré, le worker n'appelle jamais `POST /evaluate` et aucun
    signal n'est retenu à cause de CSI (ni avis, ni panne de CSI)."""

    enabled: bool = False
    hold_indetermine: bool = False
    allow_when_unavailable: bool = False

    @classmethod
    def from_mapping(cls, preferences) -> GatePolicy:
        preferences = preferences if isinstance(preferences, dict) else {}
        return cls(
            enabled=link_enabled(preferences, GATE_ENABLED_KEY),
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


# ==========================================================================
# Taille proposée par CSI : information seulement, jamais appliquée (docs/RISK_PROTOCOL.md de CSI)
# ==========================================================================


def size_advice(risk: dict | None, symbol: str, budget: float, stop_distance_pct: float | None) -> dict:
    """Ce que CSI proposerait pour ce signal, à côté du budget retenu : budget × taille relative (à risque égal
    entre paires, d'après la seule prévision de volatilité confirmée), et le stop du signal comparé à l'ampleur
    typique des 24 prochaines heures. `available` False avec `reason` quand CSI ne sait pas."""
    if not isinstance(risk, dict) or not risk.get("available"):
        reason = (risk or {}).get("reason") if isinstance(risk, dict) else None
        return {"available": False, "reason": reason or "aucun conseil de CSI"}
    pairs = risk.get("pairs")
    pair = pairs.get(str(symbol).upper()) if isinstance(pairs, dict) else None
    if not isinstance(pair, dict):
        return {"available": False, "reason": f"{str(symbol).upper()} absente de la prévision de CSI"}
    try:
        move = float(pair["move_24h_pct"])
        relative = float(pair["relative_size"])
    except (KeyError, TypeError, ValueError):
        return {"available": False, "reason": "conseil de CSI illisible"}
    if not (math.isfinite(move) and math.isfinite(relative) and move > 0 and relative > 0):
        return {"available": False, "reason": "conseil de CSI illisible"}
    if stop_distance_pct is not None and not math.isfinite(float(stop_distance_pct)):
        stop_distance_pct = None
    out = {"available": True, "origin": risk.get("origin", ""), "move_24h_pct": move, "relative_size": relative,
           "budget": float(budget), "proposed_budget": round(float(budget) * relative, 2)}
    if stop_distance_pct is not None:
        out["stop_distance_pct"] = float(stop_distance_pct)
        out["stop_inside_move"] = float(stop_distance_pct) < move
    return out


def size_advice_text(advice: dict | None, quote_asset: str = "USDT") -> str:
    if not advice:
        return ""
    if not advice.get("available"):
        return f"CSI (information, non appliqué) : {advice.get('reason') or 'indisponible'}."
    text = (f"CSI (information, non appliqué) : ampleur typique sur 24 h {advice['move_24h_pct']:.2f} %, taille "
            f"relative ×{advice['relative_size']:.2f} → budget proposé {advice['proposed_budget']:.2f} {quote_asset} "
            f"(retenu : {advice['budget']:.2f})")
    if "stop_inside_move" in advice:
        text += (f" ; stop à {advice['stop_distance_pct']:.2f} %, "
                 + ("à l'intérieur du mouvement ordinaire d'une journée" if advice["stop_inside_move"]
                    else "au-delà du mouvement ordinaire d'une journée"))
    return text + "."
