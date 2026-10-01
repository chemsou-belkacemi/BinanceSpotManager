"""Contrôle des droits d'une clé API Binance, à partir de la réponse de
`GET /sapi/v1/account/apiRestrictions` (Binance réel).

Fonction PURE, testée hors ligne : elle ne fait aucun appel réseau.

Elle n'est appelée nulle part aujourd'hui, et c'est voulu :
- le bot est verrouillé sur Binance Demo, où la route `/sapi/v1/account/apiRestrictions`
  n'existe pas ;
- le client n'appelle aucune route `/sapi` ; ajouter celle-ci élargirait les routes signées,
  ce qui n'est pas fait sur cette branche.
Elle servira le jour où le propriétaire déciderait d'un passage en réel : la clé d'un client
devrait alors être refusée AVANT tout ordre si elle permet les retraits ou ne permet pas le
trading Spot, et signalée si elle n'est pas restreinte à l'adresse IP du serveur.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: Droits sans usage pour ce bot : signalés (une clé dédiée ne devrait pas les avoir).
UNUSED_PERMISSIONS = {
    "enableMargin": "marge",
    "enableFutures": "contrats à terme",
    "enableVanillaOptions": "options",
    "enablePortfolioMarginTrading": "portefeuille sur marge",
}
#: Droits qui permettent de déplacer des fonds sans retrait : signalés.
TRANSFER_PERMISSIONS = {
    "enableInternalTransfer": "transferts internes",
    "permitsUniversalTransfer": "transferts universels entre comptes",
}


@dataclass(frozen=True)
class KeyRestrictionReport:
    """`accepted` faux = clé à refuser. `warnings` : à corriger, sans bloquer."""

    accepted: bool
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def assess_api_restrictions(payload: Any) -> KeyRestrictionReport:
    """Évalue la réponse JSON de apiRestrictions. Seule la valeur booléenne `True` vaut accord."""
    if not isinstance(payload, Mapping):
        return KeyRestrictionReport(False, ["Réponse apiRestrictions illisible : clé refusée par prudence"])

    def granted(name: str) -> bool:
        return payload.get(name) is True

    refusals: list[str] = []
    warnings: list[str] = []
    if "enableWithdrawals" not in payload or granted("enableWithdrawals"):
        refusals.append(
            "Retraits activés (ou non confirmés désactivés) : créer une clé SANS droit de retrait"
        )
    if not granted("enableSpotAndMarginTrading"):
        refusals.append("Trading Spot non autorisé sur cette clé : le bot ne pourrait passer aucun ordre")
    if not granted("enableReading"):
        refusals.append("Lecture du compte non autorisée : soldes et ordres illisibles")
    if not granted("ipRestrict"):
        warnings.append(
            "Clé non restreinte par adresse IP : la limiter à l'IP du serveur qui fait tourner le bot"
        )
    for name, label in TRANSFER_PERMISSIONS.items():
        if granted(name):
            warnings.append(f"Droit inutile et risqué activé : {label}")
    for name, label in UNUSED_PERMISSIONS.items():
        if granted(name):
            warnings.append(f"Droit inutile pour ce bot activé : {label}")
    return KeyRestrictionReport(not refusals, refusals, warnings)
