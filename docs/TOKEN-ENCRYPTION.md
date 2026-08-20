# Chiffrement des jetons au repos (APP-01)

Ferme la moitié restante du finding APP-01 de l'audit : `bearer_token`,
`refresh_token` et `firebase_api_key` suffisent à agir au nom de
l'utilisateur sur Liberty Rider, et le refresh token **ne périme pas seul**.
Un dump de base — ou une sauvegarde égarée — valait donc prise de contrôle
durable, pour deux comptes tiers autant que pour l'exploitant.

## Comment ça marche

- `crypto.py` enveloppe **Fernet** (AES-128-CBC + HMAC) avec une clé lue
  dans `TOKEN_ENCRYPTION_KEY`.
- `db.save_user_tokens()` chiffre, `db.get_session_user()` déchiffre :
  aucun appelant ne change, `user["bearer_token"]` reste du clair.
- Les valeurs stockées portent le marqueur **`enc:v1:`**. Ce qui a été écrit
  avant l'existence de la clé reste donc lisible, et est **migré en place au
  démarrage suivant** (`db.encrypt_plaintext_tokens`, idempotent).

## Déployer — l'ordre compte

La clé doit exister **avant** que le code chiffrant n'arrive, sinon rien ne
casse mais rien n'est chiffré non plus jusqu'au redémarrage suivant.

```bash
# 1. générer la clé (en local, une seule fois)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. la poser sur la VM, dans le fichier déjà root:0600 lu par systemd
sudo sh -c 'echo "TOKEN_ENCRYPTION_KEY=<la-clé>" >> /etc/roadtrips.env'
sudo chmod 600 /etc/roadtrips.env

# 3. déployer le code, installer la dépendance, redémarrer
#    (cryptography==50.0.0 est ajoutée à requirements.txt)
sudo systemctl restart roadtrips

# 4. vérifier : le log dit quel mode tourne
journalctl -u roadtrips -n 20 | grep -i "token encryption"
#   → "token encryption active (N row(s) migrated)"     ✅
#   → "TOKEN_ENCRYPTION_KEY is not set …"               ❌ la clé n'est pas lue
```

**Sauvegardez la clé hors de la VM**, dans votre gestionnaire de mots de
passe. Elle ne doit jamais vivre dans la base qu'elle protège, ni dans le
dépôt. Et le timer `pg_dump` (VM-02) produit désormais des dumps où les
jetons sont chiffrés — c'est précisément l'intérêt.

## Ce qui se passe si la clé est perdue ou changée

Les jetons deviennent illisibles : `crypto.decrypt()` renvoie `None`,
`_live_client_for` répond **401 « No Liberty Rider token on file »**, et
l'utilisateur se reconnecte. Pas de crash, pas de 500 — mais tout le monde
doit se reconnecter. C'est le compromis assumé.

Une clé **malformée**, en revanche, empêche le démarrage. C'est délibéré :
retomber silencieusement sur du stockage en clair est exactement ce que ce
module existe pour empêcher.

## Sans clé

Pas de chiffrement, valeurs en clair — c'est ce sur quoi tournent le
développement local et la suite de tests. Rendre la clé obligatoire ferait
d'une variable d'environnement oubliée une panne de login. Le démarrage
journalise un `warning` explicite dans ce cas.

## Rotation (non implémentée)

Le marqueur `enc:v1:` est là pour ça : lire avec l'ancienne clé, réécrire
avec la nouvelle, passer à `enc:v2:`. À implémenter le jour où c'est
nécessaire — aujourd'hui, changer la clé revient à déconnecter tout le monde.
