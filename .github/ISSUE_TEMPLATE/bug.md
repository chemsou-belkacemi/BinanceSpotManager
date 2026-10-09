---
name: Bug
about: Un comportement incorrect (calcul, ordre Demo, worker, interface, signaux)
title: "[bug] "
labels: bug
---

> **Une faille de sécurité ?** N'ouvrez pas d'issue publique : onglet Security → Report a vulnerability (voir `SECURITY.md`).
> N'écrivez ici aucune clé, aucun jeton, aucun mot de passe, aucun contenu de `.env`, aucun solde réel.

## Ce qui se passe

<!-- Décrivez le problème en quelques phrases. -->

## Ce qui était attendu

## Pour reproduire

1. Mode (`DRY_RUN`, `DEMO_MANUAL`, `DEMO_AUTO`) et page ou commande utilisée :
2. Paire et plan de la position (entrées, TP, SL), ou texte du signal **sans donnée personnelle** :
3. Extrait de journal utile (`logs/bot.log`, `logs/errors.log`), relu pour retirer tout secret :

```text
collez ici l'extrait
```

## Environnement

- Commit (`git rev-parse --short HEAD`) :
- Avec Docker (`make up`) ou sans (venv) ; système et version de Python :

## Vérifications

- [ ] Le problème se produit sur Binance Demo ou en `DRY_RUN` (aucun mode réel n'existe).
- [ ] J'ai relu la section « Limites connues » du README : ce n'est pas une limite déjà connue.
