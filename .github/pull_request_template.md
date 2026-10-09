## Ce qui change

<!-- Quoi, et pourquoi. Liez l'issue concernée (« Ferme #… »). -->

## Comment c'est testé

<!-- Tests ajoutés ou modifiés, commandes lancées, sortie résumée. Capture d'interface sans clé ni solde réel. -->

## Liste de contrôle

- [ ] `.venv/bin/python -m pytest -m "not integration"` vert (ou `make test`).
- [ ] Tout calcul nouveau ou corrigé de prix, de quantité, de frais, de risque ou de taille a son test.
- [ ] Aucun test ne touche le réseau hors du marqueur `integration`.
- [ ] Aucune clé, aucun jeton, aucun mot de passe, aucun fichier `.env` ajouté.
- [ ] Binance Demo uniquement : liste blanche Demo inchangée, `assert_write_allowed()` toujours appelé, aucun mode réel.
- [ ] Tout nouveau lien automatique avec CSI est désactivé par défaut.
- [ ] Aucune phrase ni aucun chiffre ne promet un gain.
- [ ] Documentation à jour (README, `docs/`), en français.
