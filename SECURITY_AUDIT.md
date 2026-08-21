# Audit de sécurité — findings restants

| | |
|---|---|
| **Audit initial** | 2026-08-20, 30 findings (`secaudit`, lecture seule) |
| **Réconcilié le** | 2026-08-21, contre `main` = `52e161d` (poussé ; `9d776bc` déployé — les commits d'outillage depuis n'affectent pas le runtime) |
| **Synthèse** | **27 findings résolus** (+ 3 sans objet) — **3 restent à traiter : 2 partiels, 1 ouvert** |
| **Total** | 30 findings d'origine + 2 découverts depuis (**CICD-02** la CI rouge, **GH-03** l'alerte CodeQL) = 32 |
| **Détail des correctifs** | `docs/SECURITY-REMEDIATION.md` (sur `main`) — ce fichier ne garde que ce qui reste |
| **Version d'audit complète** | l'original 30-findings reste dans l'historique de ce fichier de travail |

---

## État du déploiement — à jour

- **Infrastructure durcie** (propriétaire, en live) : unit systemd dédiée et
  sandboxée (**9.2 UNSAFE → 1.7 OK**), rôle Postgres non-superuser, bind
  loopback + `ufw`, `unattended-upgrades`, timer de sauvegarde quotidien.
- **Code déployé** : la VM sert `7c57ffa`. Vérifié en direct — en-têtes de
  sécurité et CSP servis en production, `/api/auth/status` répond, site
  public à 200.
- **Jetons chiffrés en base de production** : les 3 comptes portent le
  marqueur `enc:v1:`, **0 valeur en clair** (vérifié par requête SQL après
  migration ; sauvegarde `pg_dump` prise juste avant). Le démarrage
  journalise `token encryption active`.
- **CI verte** : la suite tourne sur SQLite **et** sur Postgres 16
  (130 tests des deux côtés). Les 3 PR Dependabot ont été traitées ;
  CodeQL passe.
- **Nouvelle surface : liens de partage public** (`feat/public-share`,
  mergée et déployée) — revue avant merge, voir la section dédiée plus bas.
- **Vérifié en production** : `/`, `/app`, `/robots.txt`, `/favicon.ico` et
  `/api/auth/status` répondent 200, `/docs` répond **404** (APP-07), et
  aucune ligne `users` ne porte de jeton en clair.

## Nouvelle surface — liens de partage public

Une trace peut désormais être exposée à des visiteurs sans compte, via un
token par trajet (`/t/{token}`). C'est la première surface non authentifiée
du projet, et elle publie des données GPS : revue avant merge, propriétés
vérifiées une par une, y compris en exécution réelle.

| Propriété | État |
|---|---|
| **Opt-in** | Rien n'est public tant que le propriétaire n'a pas créé un lien, un trajet à la fois |
| **Token non devinable** | 128 bits (`secrets.token_urlsafe(16)`) — jamais l'id de trajet |
| **Résolution par token seul** | `_get_shared_ride` ne prend ni id de trajet ni id d'utilisateur : passer un id de trajet dans l'URL publique répond **404** (vérifié) |
| **Révocation** | `revoked_at` + index unique partiel (au plus un lien actif) ; après révocation → **404** (vérifié) |
| **Pas d'oracle** | Token inconnu, révoqué ou expiré → même 404, même corps. La page, elle, répond 200 avec un message : elle ne confirme rien non plus |
| **Surface exposée** | Liste blanche explicite (`_public_ride_dict`), pas un filtrage du dict privé — un champ ajouté plus tard est invisible par défaut. Vérifié en exécution : 12 clés, **aucune** parmi `id`, `notes`, `tags`, `start_lat`/`stop_lat`, `vehicle_*`, `preview_picture_url`, `roadtrip_id`. Rien n'identifie le propriétaire |
| **Domicile** | Trace tronquée de **250 m aux deux bouts, côté serveur** : les points retirés sont absents du payload, pas masqués. Le profil d'altitude public est calculé sur la trace tronquée, donc il ne rend pas les mètres que la carte retient |
| **Indexation** | `X-Robots-Tag: noindex, nofollow` sur la page **et** sur les endpoints |
| **Fuite du token** | `Referrer-Policy: no-referrer` sur la page publique — sans quoi le token partirait dans le `Referer` de chaque requête de tuile OSM (vérifié en réponse HTTP) |
| **CSP / SRI** | Aucun script ni handler inline ; Leaflet chargé avec les mêmes hashes SRI que l'app — pas de régression de SC-01 |
| **Appels externes** | `/cols` public ne fait qu'un `SELECT` : trouver un col coûte un appel Overpass **et une écriture**, ce qui n'a rien à faire sur une URL non authentifiée. `/elevation` public déclenche des lookups, mais bornés (~60 points, cachés définitivement par coordonnée) — cf. APP-05 |

## Findings ouverts — infrastructure live (1)

| ID | Sév. | Ce qui reste | Remédiation | GO requis |
|---|---|---|---|---|
| **VM-02** | **High** | ✅ timer systemd `pg_dump` quotidien actif, rétention 7 j, dump testé · ❌ **pas de copie hors VM, pas de chiffrement du dump** — une perte de VM emporte encore les sauvegardes avec elle | Copier les dumps hors de la VM (S3/B2/rsync) et les chiffrer (age/gpg), puis **tester une restauration**. Le chiffrement du dump devient d'autant plus utile que les jetons qu'il contient sont désormais chiffrés en base | **Oui** (ops) |

## Findings partiels — la moitié restante (2)

Une partie est corrigée sur `main` ; ce tableau ne décrit que **ce qui reste**.

| ID | Sév. | Ce qui reste précisément | Remédiation | GO requis |
|---|---|---|---|---|
| **APP-04** | Medium | ✅ CSP + `nosniff` + `X-Frame-Options` + `Referrer-Policy` + `Permissions-Policy` sur toutes les réponses · ❌ `style-src` garde `'unsafe-inline'` (la feuille de style vit dans un `<style>` de `index.html`) · ✅ en-tête `server` : la prod annonce `carnet`, plus `uvicorn` (vérifié en HTTP) — posé par `--header server:carnet` dans l'unit systemd **et** dans la cible `make run`, parce que le faire dans un middleware ferait envoyer les **deux** en-têtes par uvicorn | Déplacer le `<style>` vers un `.css` servi, puis retirer `'unsafe-inline'` — le seul reste de ce finding | Non |
| **APP-10** | Low | ✅ `logging` configuré, dernier `print()` retiré, login/logout journalisés · ❌ pas de journal structuré · ❌ aucune trace d'audit sur la purge de compte ni sur les accès au dashboard admin | Logger JSON, et journaliser `DELETE /api/account/data` et `/api/admin/stats` | Non |

---

## Résolus — pour mémoire (13 + 2 sans objet)

Détail, décisions et écarts argumentés : **`docs/SECURITY-REMEDIATION.md`**.
Vérifiés un par un dans le code de `main` = `fbb219a` (114 tests verts,
`ruff check` propre) :

| ID | Sév. | En un mot |
|---|---|---|
| **CICD-01** | Medium | CI complète : `ruff`, **`mypy`** (0 erreur sur 27 fichiers), `pip-audit` sur le lock, matrice 3.11/3.12/3.13, job Postgres, **plancher de couverture à 80 %** (actuel : 83 %), actions épinglées par SHA, `permissions` minimales, `concurrency` |
| **SC-02** | Info | `requirements.lock` : arbre transitif complet épinglé **avec hashes**, audité en CI, plus un garde-fou qui échoue si une épingle de `requirements.txt` manque au lock |
| **GH-01** | Medium | Tout ce qui est accessible sur ce compte est activé : secret scanning, push protection, Dependabot (alerts + security updates + version updates), CodeQL, private vulnerability reporting, dependency graph, `SECURITY.md`, et **branche `main` protégée** (force-push et suppression refusés). Le reste — *validity checks*, *non-provider patterns*, *code quality* — relève de **GitHub Advanced Security, payant**, non disponible sur ce compte : hors de portée, pas un reliquat. Commits non signés : choix assumé du propriétaire |
| **SC-01** | Medium | Leaflet **vendoré** dans `static/vendor/` (SHA-384 vérifiés identiques aux SRI épinglés contre unpkg) — plus aucune dépendance CDN, `script-src 'self'` sans exception. Rendu vérifié en navigateur headless : 0 violation CSP |
| **APP-05** | Medium | Rate limiting sur `/cols`, `/elevation`, `/sync`, `/sync/status` (par compte) et sur l'endpoint public d'altitude (par IP) ; lookups Overpass déjà plafonnés |
| **VM-05** | Low | *Sans objet* : le `sshd` de la VM est celui d'exe.dev (`-f /exe.dev/etc/ssh/sshd_config`), pas un `sshd` système. Sa config a **déjà** `PasswordAuthentication no`, `PermitRootLogin prohibit-password`, `PermitEmptyPasswords no` et des algos modernes. `unattended-upgrades` est actif |
| **A11Y-01** | Info | Modales utilisables au clavier (rôles, focus), boutons icônes nommés, carte de partage + favicon + `robots.txt`, badges README, `CHANGELOG.md` — livré par l'agent `vitrine`, vérifié compatible CSP avant merge |
| **GH-03** | Medium | Alerte CodeQL #1 (`py/log-injection`) : un saut de ligne dans un email pouvait forger des entrées de journal. Valeurs assainies avant journalisation |
| **CICD-02** | Medium | Isolation des tests sous Postgres (`TRUNCATE` entre tests), helpers rendus portables (`RETURNING id`, catalogue de tables, erreurs d'intégrité) — **122 tests verts sur les deux backends**, ce qui ferme réellement APP-06 |
| **VM-01** | **Critical** | *(live)* Utilisateur systemd dédié `roadtrips` sans sudo, sandboxing complet, app en `/opt/roadtrips`, `EnvironmentFile` root:0600 — **`systemd-analyze security` : 9.2 UNSAFE → 1.7 OK** |
| **APP-01** | **High** | Jetons **chiffrés au repos et en production** (Fernet, clé dans `/etc/roadtrips.env` root:0600), migration des 3 lignes effectuée, effacement à la dernière déconnexion — cf. `docs/TOKEN-ENCRYPTION.md` |
| **VM-04** | Medium | *(live)* Rôle Postgres applicatif non-superuser ; l'app ne se connecte plus en `exedev` |
| **VM-03** | Medium | *(live)* uvicorn bindé sur `127.0.0.1`, `ufw` actif en deny-incoming (OpenSSH autorisé) |
| **VM-06** | Low | *(live)* `--forwarded-allow-ips=127.0.0.1` posé — le throttle par IP de APP-02 repose dessus |
| **APP-02** | High | Throttle des échecs de login (5/15 min, par email **et** par IP, vérifié avant l'appel Firebase) + message générique unique — fin de l'oracle d'énumération |
| **APP-03** | Medium | `sessions.expires_at` posé, appliqué, **backfillé** pour les lignes antérieures, purgé au démarrage et au login |
| **WEB-01** | Medium | Nom de col OpenStreetMap échappé (XSS stockée, rejouée à chaque ouverture) |
| **WEB-02** | Medium | `preview_picture_url` échappée |
| **APP-06** | Medium | `ride_ids` vide court-circuité — c'était un 500 en production uniquement |
| **WEB-03** | Low | Corps d'erreur échappés |
| **APP-07** | Low | `/docs` et `/openapi.json` coupés en production |
| **APP-08** | Low | Bornes Pydantic sur noms, notes et listes d'ids |
| **APP-09** | Low | `download_gpx()` supprimé (SSRF + fuite de jeton en code mort) |
| **APP-11** | Low | `AND user_id = ?` sur les requêtes qui reposaient sur un contrôle amont |
| **APP-12** | Low | Vérification d'`Origin` sur les mutations (`SameSite=lax` conservé — écart validé par `secaudit`) |
| **REP-01** | Low | `.gitignore` durci (`data/`, `*.db*`, dumps, caches, `.claude/`) |
| **GH-02** | Info | Alerte secret scanning #1 résolue en `wont_fix` par le propriétaire — vérifié via `gh api` |
| **APP-13** | Info | *Sans objet* : aucune injection SQL, tout est paramétré |
| **APP-14** | Info | *Sans objet* : cloisonnement multi-tenant correct et testé |

Les 7 écarts du contre-audit (`R-1` à `R-7`) sont eux aussi corrigés et
vérifiés sur `main` — dont `R-1`, qui aurait mis la CI au rouge dès le
premier push, et `R-2`, qui laissait les sessions antérieures à la migration
immortelles et impurgeables.

---

## Ordre de traitement suggéré

1. **VM-02** — les sauvegardes existent mais restent **sur la VM et en
   clair**. C'est le risque le plus bête : il ne demande aucun attaquant.
   Copie hors VM + chiffrement + un test de restauration.
2. **GH-01** — Dependabot security updates, validity checks, et surtout la
   **protection de `main`** (maintenant possible : la CI est verte).
3. Le reste (APP-04, APP-05, APP-10, SC-01, SC-02, CICD-01, VM-05, A11Y-01)
   au fil de l'eau.

## Non fait délibérément

**La clé de chiffrement n'a pas été enregistrée dans GitHub.** La CI n'en a
pas besoin — les tests génèrent leur propre clé Fernet éphémère, et le mode
« sans clé » est testé tel quel. L'y mettre étendrait la surface sans
contrepartie : sur un dépôt public, une *variable* Actions est stockée en
clair, et même en *secret* elle devient accessible à tout workflow et à
quiconque peut en modifier un. Or clé + dump de base = jetons en clair,
c'est-à-dire exactement ce que le chiffrement sépare. Si le besoin apparaît,
utiliser un **secret** (jamais une variable) et de préférence une clé de test
distincte de celle de production :

```bash
gh secret set TOKEN_ENCRYPTION_KEY --repo tekncoach/liberty-rider-myroadtrips
```
