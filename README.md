# snowflake-admin — pipelines CI/CD Snowflake

Ce dépôt héberge la **logique** de lint et de déploiement Snowflake, sous
forme de [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows)
GitHub Actions. Le code SQL à déployer, lui, vit dans un autre dépôt :
**[amine-ds/snowflake-git](https://github.com/amine-ds/snowflake-git)**.

## 1. Pourquoi deux dépôts, et comment ils communiquent

- `snowflake-git` contient uniquement le code SQL (`sql/**`) et les 3
  branches longues `dev`, `qual`, `prod`.
- `snowflake-admin` (ce dépôt) contient les scripts (`scripts/lint_sql.py`,
  `scripts/deploy_snowflake.py`) et le workflow réutilisable
  (`.github/workflows/snowflake-deploy.yml`). C'est aussi **ici** que
  vivent les Environments GitHub `qual` et `prod` (secrets + validation
  obligatoire pour prod) — pas dans `snowflake-git`.
- Un petit workflow "appelant", situé dans `snowflake-git`
  (`.github/workflows/snowflake-cicd.yml`), invoque ce workflow réutilisable
  à chaque PR vers `qual` ou `prod` :
  ```yaml
  jobs:
    deploy:
      uses: amine-ds/snowflake-admin/.github/workflows/snowflake-deploy.yml@main
      with:
        target_env: QUA        # ou PRD
        environment_name: qual # ou prod
  ```
- Un `workflow_call` hérite automatiquement du contexte `github.*` (dépôt,
  ref, SHA, événement) de l'appelant. C'est pour ça qu'un simple
  `actions/checkout@v4` **sans** `repository:` explicite, dans le workflow
  réutilisable, checkoute bien le code SQL de `snowflake-git` — pas de
  `snowflake-admin`. Les scripts Python de ce dépôt sont ensuite checkoutés
  séparément, dans un sous-dossier `_admin/` (deuxième `actions/checkout`
  avec `repository: amine-ds/snowflake-admin`), pour pouvoir être exécutés
  contre les fichiers SQL du premier checkout.
- Les secrets utilisés dans le job `deploy` du workflow réutilisable
  (`${{ secrets.SNOWFLAKE_ACCOUNT }}`, etc.) sont résolus à partir de
  l'Environment GitHub (`qual`/`prod`) défini **dans ce dépôt**, puisque
  c'est ce dépôt qui possède le fichier de workflow où le job est déclaré —
  `snowflake-git` n'a donc jamais besoin de connaître le moindre secret
  Snowflake.

## 2. Modèle de promotion par branches (côté snowflake-git)

```
prod ──(create branch TICKET-123)──> TICKET-123
                                         │
                                         ▼  (merge, aucune pipeline)
                                        dev
                                         │
                                         ▼  (PR vers qual : lint + déploiement
                                         │   automatique sur QUA, sans validation)
                                        qual
                                         │
                                         ▼  (PR vers prod : lint + déploiement
                                             sur PRD, validation manuelle requise)
                                        prod
```

- Une branche de ticket est créée **depuis `prod`** (jamais depuis `dev` ou
  `qual`), nommée généralement d'après le numéro de ticket.
- Merge dans `dev` : **aucune pipeline ne se déclenche**. Le développeur a
  déjà testé manuellement sur son propre environnement DEV avant ce merge.
- PR vers `qual` : lint automatique, puis déploiement automatique sur
  l'environnement Snowflake `QUA`, **sans validation manuelle**.
- PR vers `prod` : lint automatique, puis déploiement sur `PRD`, bloqué
  tant qu'un reviewer n'a pas cliqué "Approve and deploy" sur l'Environment
  GitHub `prod` (configuré dans ce dépôt, voir section 3).

Comme pour l'ancien modèle à une seule branche, le déploiement a lieu
**avant** le merge (sur la PR elle-même) : la PR reste ouverte le temps que
le déploiement qual (et, le cas échéant, prod) réussisse, avant d'être
mergée.

## 3. Mise en place

### 3.1 Comptes de service Snowflake

Deux comptes de service suffisent (aucun déploiement automatique ne va sur
DEV) :

```sql
-- Compte QUAL : accès uniquement aux bases QUA_*
CREATE USER SVC_CICD_QUAL
  RSA_PUBLIC_KEY = '<clé publique PEM>'
  DEFAULT_ROLE = ROLE_CICD_QUAL
  DEFAULT_WAREHOUSE = <NOM_WAREHOUSE_QUAL>
  MUST_CHANGE_PASSWORD = FALSE;

CREATE ROLE ROLE_CICD_QUAL;
GRANT USAGE ON WAREHOUSE <NOM_WAREHOUSE_QUAL> TO ROLE ROLE_CICD_QUAL;
GRANT ALL ON DATABASE QUA_<ZONE> TO ROLE ROLE_CICD_QUAL;  -- une ligne par zone (brz/slv/gld) à autoriser
GRANT ROLE ROLE_CICD_QUAL TO USER SVC_CICD_QUAL;

-- Compte PROD : accès uniquement aux bases PRD_*
CREATE USER SVC_CICD_PROD
  RSA_PUBLIC_KEY = '<clé publique PEM>'
  DEFAULT_ROLE = ROLE_CICD_PROD
  DEFAULT_WAREHOUSE = <NOM_WAREHOUSE_PROD>
  MUST_CHANGE_PASSWORD = FALSE;

CREATE ROLE ROLE_CICD_PROD;
GRANT USAGE ON WAREHOUSE <NOM_WAREHOUSE_PROD> TO ROLE ROLE_CICD_PROD;
GRANT ALL ON DATABASE PRD_<ZONE> TO ROLE ROLE_CICD_PROD;  -- une ligne par zone (brz/slv/gld) à autoriser
GRANT ROLE ROLE_CICD_PROD TO USER SVC_CICD_PROD;
```

Génération de la paire de clés (à faire une fois, en local, jamais commitée) :

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
```
`rsa_key.pub` (sans les en-têtes `-----BEGIN/END-----`) va dans
`RSA_PUBLIC_KEY` côté Snowflake ; `rsa_key.p8` va dans le secret GitHub
`SNOWFLAKE_PRIVATE_KEY`.

> Important : chaque rôle ne doit avoir **aucun droit** sur les objets de
> l'autre environnement (vérifier qu'aucun grant transverse n'existe, p. ex.
> sur un rôle public).

### 3.2 GitHub Environments — DANS CE DÉPÔT (snowflake-admin)

`Settings > Environments` (sur `amine-ds/snowflake-admin`, pas sur
`snowflake-git`) → créer deux environnements :

**`qual`**
- Pas de "Required reviewers" (déploiement automatique, sans validation).
- Environment secrets : `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER` (`SVC_CICD_QUAL`),
  `SNOWFLAKE_PRIVATE_KEY`, `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` (si clé
  chiffrée), `SNOWFLAKE_ROLE` (`ROLE_CICD_QUAL`), `SNOWFLAKE_WAREHOUSE`.

**`prod`**
- **Required reviewers** : ajouter le ou les comptes GitHub des admins
  Snowflake habilités à valider le déploiement prod.
- Ne PAS utiliser "Deployment branches and tags" restreint à une branche :
  pour un job déclenché par une PR, la branche du run est
  `refs/pull/<n>/merge`, jamais `prod` littéralement — cette restriction
  rejetterait tous les déploiements (piège déjà rencontré sur l'ancien
  modèle à une seule branche). Laisser "No restriction".
- Environment secrets : mêmes noms que ci-dessus, avec les valeurs du
  compte `SVC_CICD_PROD` / `ROLE_CICD_PROD`.

### 3.3 Dans snowflake-git

Voir le README de `snowflake-git` pour : la structure `sql/<brz|slv|gld>/`,
le format attendu des scripts (variable `vEnv`, en-tête de commentaire), et
le workflow appelant.

## 4. Fonctionnement du lint (`scripts/lint_sql.py`)

Bloque la PR si un fichier `.sql` modifié contient :
- un `SELECT *` (hors `COUNT(*)`)
- pas de variable `vEnv` déclarée (l'environnement doit être piloté par
  variable, jamais codé en dur dans les `FROM`/`JOIN`)
- un nom de base/schéma en dur du type `DEV_...`/`QUA_...`/`PRD_...`
  ailleurs que dans la déclaration de variable
- un warehouse (`WAREHOUSE = ...`) dont le nom n'est pas préfixé par un
  code d'environnement reconnu (`DEV_.../QUA_.../PRD_...`) — voir section 5
- pas d'en-tête de commentaire (Auteur / Date / Description)
- un mot de passe ou une clé privée écrits en clair dans le script

`DEV` est un préfixe reconnu par le lint (un script venant de la branche
`dev` porte souvent encore cette valeur), mais le déploiement ne cible
jamais DEV lui-même : voir section 5.

## 5. Fonctionnement du déploiement (`scripts/deploy_snowflake.py`)

Les scripts contiennent une ligne `vEnv varchar := 'DEV'` (ou `'QUA'`,
`'PRD'`) que le développeur modifie parfois à la main. Pour éviter une
erreur humaine, **le script de déploiement réécrit automatiquement cette
valeur** en fonction du job qui l'exécute (`QUA` pour le job qual, `PRD`
pour le job prod), quelle que soit la valeur présente dans le fichier —
DEV compris.

**Même principe pour les warehouses.** Les tables dynamiques et les tasks
référencent leur warehouse en dur (`WAREHOUSE = DEV_TRF_TRAFIC_WH` par
exemple) : cette clause n'accepte pas une variable de session en DDL, donc
le nom reste toujours écrit en clair. Le déploiement repère chaque préfixe
d'environnement reconnu (`DEV_`/`QUA_`/`PRD_`) dans une clause
`WAREHOUSE = ...` et le réécrit pour correspondre au job en cours.

Chaque fichier `.sql` modifié par la PR est envoyé à Snowflake en une seule
requête (`cur.execute`), car les scripts sont des blocs anonymes
`DECLARE ... BEGIN ... END;` qui doivent être exécutés d'un bloc.

### Gestion des dépendances entre scripts

Quand une PR modifie plusieurs fichiers `.sql` qui dépendent les uns des
autres, l'ordre d'exécution compte. Le déploiement applique deux règles,
dans cet ordre :

1. **Zone** : `sql/brz/...`, `sql/slv/...`, `sql/gld/...` sont toujours
   déployées dans cet ordre (bronze → silver → gold), quel que soit
   l'ordre renvoyé par `git diff`.
2. **Au sein d'une même zone** (ou pour tout cas particulier), déclarer la
   dépendance **dans l'en-tête du script qui en a besoin** :
   ```sql
   --DEPLOY_AFTER: sql/slv/autre_script.sql
   ```
   (plusieurs chemins possibles, séparés par des virgules). Le déploiement
   fait un tri topologique : le(s) fichier(s) listé(s) sont déployés avant
   celui qui les déclare, et une dépendance circulaire fait échouer le job
   avec une erreur explicite. Le lint vérifie que le chemin référencé
   existe bien dans le repo `snowflake-git`.

   Une dépendance vers un objet déployé lors d'une PR **précédente** (donc
   déjà en base) n'a pas besoin d'être déclarée : ce fichier n'apparaît pas
   dans la liste des fichiers modifiés de la PR courante.

## 6. Ce qui reste à décider avec l'équipe

- **Rollback** : les scripts sont `CREATE OR REPLACE` / `UPDATE`, donc pas
  de rollback automatique. À minima, consigner le lien du run GitHub
  Actions dans le ticket associé.
- **Merge automatique ou manuel** après succès du déploiement prod.
- **Conflits entre tickets concurrents** promus en parallèle vers `qual` :
  comme `qual` est une branche longue partagée, deux PR ouvertes en même
  temps vers `qual` peuvent se marcher dessus si elles touchent les mêmes
  fichiers — à gérer par l'ordre de merge, comme pour tout conflit Git
  classique.
