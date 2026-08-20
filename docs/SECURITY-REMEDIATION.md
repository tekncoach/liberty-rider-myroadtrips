# Remédiation de l'audit de sécurité

Réponse à `SECURITY_AUDIT.md` (audit du 2026-08-20, 24 findings).
Travaux réalisés par `teamlead` sur la branche **`chore/security-hardening`**,
dans un worktree isolé (`../.worktrees/security-hardening`) pour ne pas
entrer en collision avec la branche `fix/sync-status` ni avec les autres
agents travaillant sur le dépôt.

**Rien n'a été poussé, aucune PR n'a été ouverte, aucune modification n'a été
appliquée à l'infrastructure live** (VM exe.dev, Postgres de production,
réglages GitHub). Trois commits locaux, 94 tests verts, `ruff check` propre.

---

## 🚨 À lire en premier — alerte secret scanning #1

Le secret scanning a été activé sur le dépôt pendant l'audit et a
immédiatement ouvert l'**alerte #1** : `google_api_key` détectée à
**`app.py:38`** (`DEFAULT_FIREBASE_API_KEY = "AIzaSy…rgDL5c"`),
`publicly_leaked=true`.

**Ce n'est pas une fuite de secret du projet.** C'est la clé web Firebase de
Liberty Rider, publique par conception (elle identifie un projet Firebase,
elle n'autorise rien à elle seule), capturée depuis leur webapp et
documentée comme telle dans `app.py:35-38` et `.env.example`. Le détecteur
GitHub remonte tout littéral `AIzaSy…` sans distinguer clé web et clé
serveur.

Deux décisions t'appartiennent :

1. **Résoudre l'alerte dans l'UI GitHub** (`used_in_tests` / `won't fix`)
   **avec un commentaire** renvoyant au commentaire de `app.py` — pas la
   fermer en silence.
2. **Optionnel, plus propre** : sortir le littéral du code et rendre
   `LIBERTY_RIDER_FIREBASE_API_KEY` obligatoire, pour ne plus republier la
   clé d'un tiers. **Je ne l'ai pas fait** : appliqué seul, ce changement
   casse le login en production tant que la variable n'est pas définie sur
   la VM. La séquence sûre est dans la checklist ci-dessous.

Note de workflow : la **push protection est maintenant active**. Tout commit
ou toute réécriture d'historique réintroduisant ce littéral sera bloqué et
demandera un bypass explicite.

---

## (a) Ce qui est corrigé — branche `chore/security-hardening`

### Commit 1 — `a21e729` XSS, en-têtes, bornes d'entrée

| Finding | Sév. | Correctif | Fichiers |
|---|---|---|---|
| **WEB-01** | Medium | `escapeHtml()` sur le nom de col issu d'OpenStreetMap (donnée éditable par n'importe qui, persistée dans `ride_cols`, rejouée à chaque ouverture) | `static/app.js` |
| **WEB-02** | Medium | `escapeHtml()` sur `preview_picture_url` (donnée d'API tierce) | `static/app.js` |
| **WEB-03** | Low | `escapeHtml()` sur les corps d'erreur bruts (le dashboard admin le faisait déjà : incohérence fermée) | `static/app.js` |
| **APP-04** | Medium | Middleware posant CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` sur **toutes** les réponses. HSTS reste au proxy (le poser depuis un serveur de dev en HTTP est un piège) | `app.py` |
| **SC-01** | Medium | Leaflet épinglé avec **SRI + `crossorigin`** — partiel, voir « restant » | `static/index.html` |
| **APP-12** | Low | Vérification d'`Origin` explicite sur POST/PUT/PATCH/DELETE | `app.py` |
| **APP-07** | Low | `/docs`, `/redoc`, `/openapi.json` coupés en production (`COOKIE_SECURE=1`), réactivables par `EXPOSE_API_DOCS=1` | `app.py` |
| **APP-08** | Low | Contraintes Pydantic : noms ≤ 200, notes ≤ 10 000, listes d'ids ≤ 1 000 | `app.py` |
| **APP-06** | Medium | Liste `ride_ids` vide court-circuitée avant de construire `IN ()` — **c'était un 500 en production uniquement** | `app.py` |
| **APP-11** | Low | `AND user_id = ?` ajouté aux 5 requêtes qui reposaient uniquement sur un contrôle de propriété en amont | `app.py` |

**Décision argumentée — `SameSite`** : l'audit proposait `strict`. Je suis
resté en **`lax`** : `strict` empêcherait l'envoi du cookie à l'arrivée sur
`/app` depuis un lien externe (WhatsApp, email), donc écran de login au
premier affichage pour un utilisateur pourtant connecté. La protection
explicite passe par la vérification d'`Origin`, qui ne coûte rien côté UX.
Les requêtes sans `Origin` (curl, tests, clients non-navigateur) ne sont pas
bloquées : l'en-tête est un signal de navigateur, pas un mécanisme
d'authentification.

### Commit 2 — `417abd9` Login, sessions, jetons

| Finding | Sév. | Correctif | Fichiers |
|---|---|---|---|
| **APP-02** | **High** | Comptage des **échecs** de login par email **et** par IP (5 / 15 min), vérifié **avant** l'appel Firebase (un flood ne coûte donc rien et ne martèle pas Firebase depuis notre IP) ; **message unique générique** « Identifiants invalides » à la place du message Firebase qui distinguait `EMAIL_NOT_FOUND` de `INVALID_PASSWORD` (oracle d'énumération de comptes) | `app.py` |
| **APP-03** | Medium | `sessions.expires_at` posé à la création et **appliqué dans la requête** ; `delete_expired_sessions()` pour la purge ; `delete_user_sessions()` pour un futur « déconnecter partout ». Les lignes antérieures à la colonne restent valides : un déploiement ne doit pas déconnecter tout le monde | `db.py`, `migrations/0002_session_expiry.sql` |
| **APP-01** | **High** | *Partiel* : la déconnexion efface `bearer_token` **et** `refresh_token`. Le refresh token ne périme pas seul et peut régénérer des jetons Liberty Rider indéfiniment | `app.py`, `db.py` |

Le compteur de login est un dict en mémoire, **volontairement** : le service
tourne sur un seul worker uvicorn, et un dict est honnête là où une table
ferait croire à un compteur distribué. À revoir si le déploiement passe à
plusieurs workers (noté dans le code).

### Commit 3 — `8d05975` CI, outillage, appels externes

| Finding | Sév. | Correctif | Fichiers |
|---|---|---|---|
| **CICD-01** | Medium | `permissions: contents: read`, `concurrency` + `cancel-in-progress`, **actions épinglées par SHA** (un tag peut être déplacé sous nos pieds), matrice 3.11/3.12/3.13, `ruff check`, `pip-audit`, et **un job `postgres:16`** rejouant toute la suite avec `DATABASE_URL` | `.github/workflows/ci.yml` |
| **APP-06** (cause racine) | Medium | Le job Postgres ci-dessus : la prod est Postgres et la suite ne tournait que sur SQLite — la traduction `?`→`%s` de `db.py` n'était validée par rien | idem |
| **GH-01** | Medium | *Partiel (volet fichiers)* : `SECURITY.md` (périmètre réel + ce que l'app stocke), `.github/dependabot.yml` (pip + github-actions), `.github/workflows/codeql.yml` (python + javascript, `security-extended`) | 3 nouveaux fichiers |
| **APP-09** | Low | `download_gpx()` **supprimé** (pas seulement gardé) : il faisait un GET sur une URL fournie par l'API tierce avec l'en-tête `Authorization: Bearer <jeton utilisateur>` attaché, et rien ne l'appelait | `liberty_client.py` |
| **APP-05** | Medium | *Partiel* : nombre de pics analysés par requête plafonné (`MAX_PEAK_LOOKUPS = 12`) — un appel Overpass à 15 s de timeout par pic candidat, sur un worker unique | `app.py` |
| **APP-10** | Low | *Partiel* : `logging` configuré, dernier `print()` de production remplacé, échecs de login et déconnexions journalisés | `app.py`, `liberty_client.py` |
| **REP-01** | Low | `.gitignore` étendu : `data/`, `*.db*`, dumps, caches d'outils, `.claude/` | `.gitignore` |
| **SC-02** | Info | Dépendances de dev épinglées (`pytest`, `httpx`, `ruff`, `pip-audit`), `pip-audit` en CI | `requirements-dev.txt` |
| **VM-01 / VM-03** | **Critical** / Medium | **Fichier de référence non appliqué** : `deploy/roadtrips.service`, unit durcie (`NoNewPrivileges`, `ProtectSystem=strict`, `SystemCallFilter=@system-service`, `CapabilityBoundingSet=` vide, `UMask=0077`…), bind sur `127.0.0.1`, `--forwarded-allow-ips`, et les prérequis dans l'en-tête du fichier | `deploy/roadtrips.service` |

**Outillage — deux choix explicites** :
- `ruff format --check` **n'est pas** dans la CI. Reformater tout le dépôt
  produirait un diff massif qui entrerait en collision avec `fix/sync-status`
  et la future branche de partage public. À faire dans un commit isolé une
  fois ces branches mergées (c'est l'ordre que recommande l'audit lui-même).
- Les faux positifs `ruff` sont **argumentés, pas noyés** : `B008` est
  l'idiome FastAPI (`Depends(...)` en défaut) ; `S608` est restreint à
  `app.py`/`db.py`, où chaque f-string n'interpole qu'un *nombre* de
  placeholders, jamais une valeur — le reste du dépôt reste vérifié. Les
  vrais signalements ont été corrigés (chaînage `raise … from e`).

### Tests

`tests/test_security_hardening.py` — 12 tests, chacun nommant le finding
qu'il ferme : présence des en-têtes, contenu de la CSP, rejet d'une mutation
cross-site, throttling après 5 échecs, message d'erreur non discriminant,
session expirée refusée, session antérieure à la colonne toujours valide,
purge des sessions expirées, effacement des jetons à la déconnexion,
`ride_ids` vide accepté sans requête cassée, texte surdimensionné rejeté,
`/docs` absent en production.

**Suite complète : 94 tests verts** (82 existants + 12), `ruff check`
propre.

---

## (b) Ce qui attend ton GO — commandes prêtes

> Rien ci-dessous n'a été exécuté.

### 1. Pousser la branche et ouvrir la PR

```bash
git push -u origin chore/security-hardening
gh pr create --assignee pifleo --base main --head chore/security-hardening \
  --title "Security hardening: remediation of SECURITY_AUDIT.md" \
  --body-file docs/SECURITY-REMEDIATION.md
```

### 2. Activer l'outillage GitHub (~10 min, gratuit sur un dépôt public)

```bash
REPO=tekncoach/liberty-rider-myroadtrips

# Secret scanning + push protection + Dependabot security updates
gh api -X PATCH repos/$REPO -f 'security_and_analysis[secret_scanning][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled' \
  -f 'security_and_analysis[dependabot_security_updates][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_validity_checks][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_non_provider_patterns][status]=enabled'

# Private vulnerability reporting (référencé par SECURITY.md)
gh api -X PUT repos/$REPO/private-vulnerability-reporting

# Protection de main : PR obligatoire, CI verte, pas de force-push
gh api -X PUT repos/$REPO/branches/main/protection \
  -f 'required_status_checks[strict]=true' \
  -f 'required_status_checks[contexts][]=quality' \
  -f 'required_status_checks[contexts][]=test-postgres' \
  -f 'required_pull_request_reviews[required_approving_review_count]=0' \
  -f 'enforce_admins=false' -f 'restrictions=null' \
  -f 'allow_force_pushes=false' -f 'allow_deletions=false'
```

⚠️ Active la protection de `main` **après** avoir mergé la PR, sinon tu
devras passer par une PR pour ton propre travail en cours.

### 3. Résoudre l'alerte secret scanning #1

Via l'UI (Security → Secret scanning → alerte #1) : `won't fix`, en
commentant « clé web Firebase publique de Liberty Rider, cf. `app.py:35-38`
et `.env.example` ».

Puis, si tu veux retirer le littéral (dans cet ordre, sinon le login casse) :

```bash
# 1. définir la variable sur la VM AVANT de déployer le code
ssh liberty-rider-myroadtrips.exe.xyz \
  "echo 'LIBERTY_RIDER_FIREBASE_API_KEY=AIzaSy…' | sudo tee -a /etc/roadtrips.env"
# 2. puis seulement supprimer DEFAULT_FIREBASE_API_KEY de app.py et déployer
```

### 4. Infra live — VM exe.dev (aucune commande n'a été jouée)

| # | Finding | Action |
|---|---|---|
| 1 | **VM-01** *Critical* | Créer l'utilisateur dédié (`sudo adduser --system --group roadtrips`), installer `deploy/roadtrips.service`, `systemctl daemon-reload && systemctl restart roadtrips`, vérifier avec `systemd-analyze security roadtrips` (9.2 UNSAFE avant). Retirer `NOPASSWD:ALL` du compte qui fait tourner le service |
| 2 | **VM-04** | Rôle Postgres applicatif non-superuser, non-propriétaire, puis `DATABASE_URL` pointant dessus : `CREATE ROLE roadtrips_app LOGIN PASSWORD '…'; GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO roadtrips_app;` |
| 3 | **VM-02** *High* | Sauvegardes : timer systemd quotidien `pg_dump -Fc roadtrips` → chiffré (age/gpg) → copié **hors VM**, rétention 7/30 j, **et un test de restauration**. 72 Mo, quelques minutes de travail |
| 4 | **VM-03/VM-06** | Le bind loopback et `--forwarded-allow-ips` sont déjà dans le fichier d'unit ; activer `ufw` en deny-par-défaut entrant (22 + port du proxy) |
| 5 | **VM-05** | `systemctl unmask unattended-upgrades`, drop-in `/etc/ssh/sshd_config.d/` avec `PasswordAuthentication no` + `PermitRootLogin prohibit-password` |
| 6 | **APP-03** | Après déploiement, jouer `migrations/0002_session_expiry.sql` (automatique au démarrage via `_run_postgres_migrations`) |

### 5. Chiffrement des jetons en base — **APP-01, la moitié restante**

Nécessite une clé (injectée par `systemd LoadCredential=`), une migration de
données en production et une rotation. C'est le seul finding *High* qui reste
entièrement ouvert côté données. À cadrer ensemble : trois comptes réels sont
concernés, dont deux tiers (donc enjeu RGPD, pas seulement technique).

---

## (c) Findings restants et pourquoi

| Finding | Statut | Pourquoi |
|---|---|---|
| **VM-01, VM-02, VM-03, VM-04, VM-05, VM-06** | Non appliqués | Infra live : hors de mon périmètre autonome. Le durcissement systemd est livré en fichier prêt à installer |
| **GH-01** (activations) | Non appliqué | Réglages GitHub : commandes fournies, déclenchement laissé au propriétaire |
| **APP-01** (chiffrement) | Ouvert | Clé + migration de données de production + rotation |
| **APP-05** (rate limiting) | Partiel | Le plafond de pics est posé ; le rate limiting par utilisateur sur `/cols`, `/elevation`, `/sync` reste à faire, tout comme le passage de `/cols` en POST (un GET qui écrit) — changement d'API à coordonner avec le frontend |
| **APP-05** (`/api/sync/status`) | À reprendre plus tard | L'endpoint n'existe pas sur `main` : il arrive avec `fix/sync-status`. À plafonner **après** le merge de cette branche. Il n'est appelé qu'à l'ouverture et après un sync — jamais en polling |
| **SC-01** (vendoring Leaflet) | Partiel | SRI posé, ce qui ferme le risque d'altération. Vendorer supprimerait en plus la dépendance réseau tierce — ~160 Ko à committer, décision de goût à trancher |
| **APP-10** (observabilité) | Partiel | Journalisation structurée + traces d'audit complètes (purge, accès admin) restent à faire |
| **SC-02** (lock des transitives) | Partiel | Passer à `uv lock` / `pip-compile --generate-hashes` est un changement d'outillage à part entière |
| **§ 6-7** (mypy, `pyproject` complet, découpage de `app.py`, badges, `CHANGELOG`, Docker, a11y) | Non traités | Qualité/vitrine, hors périmètre sécurité — l'audit les classe en « fond » |
| **APP-13, APP-14** | Rien à faire | Aucune injection SQL, cloisonnement multi-tenant correct et testé |
