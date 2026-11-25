# snowflake-admin — scripts CI/CD Snowflake

Ce dépôt héberge le **code** des pipelines de lint et de déploiement
Snowflake (`scripts/lint_sql.py`, `scripts/deploy_snowflake.py`), mutualisé
entre les dépôts qui en ont besoin. Le code SQL à déployer, les workflows
GitHub Actions, les Environments et les secrets, eux, vivent dans le(s)
dépôt(s) appelant(s), par exemple :
**[amine-ds/snowflake-git](https://github.com/amine-ds/snowflake-git)**.

## 1. Pourquoi deux dépôts, et comment ils communiquent

- `snowflake-git` contient le code SQL (`sql/**`), les 3 branches longues
  `DEV`/`QUAL`/`PRD`, **et** son propre workflow GitHub Actions avec les
  Environments `qual`/`prod`, leurs secrets Snowflake et la validation
  obligatoire sur `prod` : c'est le dépôt qui possède les données qu'il
  déploie, donc c'est lui qui possède les accès et les validations qui en
  découlent.
- `snowflake-admin` (ce dépôt) ne contient que les scripts Python. Il n'a
  **pas** son propre workflow GitHub Actions.

  > Pourquoi pas un [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows)
  > (`workflow_call`) ? C'est l'option naturelle pour mutualiser un
  > workflow entre dépôts, mais GitHub interdit la clé `environment:` sur
  > le job qui appelle un reusable workflow (seules `name`, `uses`, `with`,
  > `secrets`, `needs`, `if`, `concurrency` et `permissions` sont
  > autorisées) — essayer quand même fait échouer la résolution du run
  > entier avant même de démarrer un job ("No jobs were run", sans
  > erreur affichée). Comme on veut que l'Environment (et donc les
  > secrets + la validation prod) reste porté par `snowflake-git`, cette
  > voie est fermée : seul le job qui exécute réellement les étapes peut
  > porter `environment:`, pas un job `uses:`.

- Le workflow de `snowflake-git` checkoute donc **deux dépôts** dans
  chaque job : lui-même (pour les fichiers `sql/`), et `snowflake-admin`
  dans un sous-dossier `_admin/` (second `actions/checkout`, avec
  `repository: amine-ds/snowflake-admin`), pour exécuter
  `_admin/scripts/lint_sql.py` et `_admin/scripts/deploy_snowflake.py`
  contre les fichiers du premier checkout.

## 2. Modèle de promotion par branches (côté snowflake-git)

```
PRD ──(create branch TICKET-123)──> TICKET-123
                                        │
                                        ▼  (merge, aucune pipeline)
                                       DEV
                                        │
                                        ▼  (PR vers QUAL : lint + déploiement
                                        │   automatique sur QUA, sans validation)
                                       QUAL
                                        │
                                        ▼  (PR vers PRD : lint + déploiement
                                            sur PRD, validation manuelle requise)
                                       PRD
```

(Les noms de branches `DEV`/`QUAL`/`PRD` sont en majuscules ; les
Environments GitHub `qual`/`prod` et les codes d'environnement Snowflake
`QUA`/`PRD` sont des identifiants distincts, choisis indépendamment.)

- Une branche de ticket est créée **depuis `PRD`** (jamais depuis `DEV` ou
  `QUAL`), nommée généralement d'après le numéro de ticket.
- Merge dans `DEV` : **aucune pipeline ne se déclenche**. Le développeur a
  déjà testé manuellement sur son propre environnement DEV avant ce merge.
- PR vers `QUAL` : lint automatique, puis déploiement automatique sur
  l'environnement Snowflake `QUA`, **sans validation manuelle**.
- PR vers `PRD` : lint automatique, puis déploiement sur `PRD`, bloqué
  tant qu'un reviewer n'a pas cliqué "Approve and deploy" sur l'Environment
  GitHub `prod` (configuré dans `snowflake-git`, voir son README section 5).

Comme pour l'ancien modèle à une seule branche, le déploiement a lieu
**avant** le merge (sur la PR elle-même) : la PR reste ouverte le temps que
le déploiement qual (et, le cas échéant, prod) réussisse, avant d'être
mergée.

## 3. Mise en place

Tout le setup (comptes de service Snowflake, clés RSA, Environments GitHub
`qual`/`prod`, secrets, workflow GitHub Actions) se fait **dans le dépôt
appelant** (`snowflake-git`), puisque c'est lui qui les possède — voir son
README, section 5. Ce dépôt (`snowflake-admin`) n'a besoin d'aucune
configuration de secret ou d'Environment : il est checkouté en lecture
seule par le workflow du dépôt appelant.

Pour qu'un autre dépôt utilise ces mêmes scripts, il lui suffit de
reproduire le même schéma de workflow (checkout du dépôt + checkout
secondaire de `snowflake-admin` dans `_admin/`, voir section 1), avec ses
propres Environments et secrets.

## 4. Fonctionnement du lint (`scripts/lint_sql.py`)

Bloque la PR si un fichier `.sql` modifié contient :
- un `SELECT *` (hors `COUNT(*)`)
- pas de variable `vEnv` déclarée (l'environnement doit être piloté par
  variable, jamais codé en dur dans les `FROM`/`JOIN`)
- un nom de base en dur (`DEV`/`QUA`/`PRD` utilisé tel quel) ailleurs que
  dans la déclaration de la variable `vEnv` — une seule base par
  environnement, les zones (`BRZ`/`SLV`/`GLD`) sont des schémas à
  l'intérieur, pas des bases séparées
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

**Même principe pour les warehouses, avec une convention différente.** Les
tables dynamiques et les tasks référencent leur warehouse en dur
(`WAREHOUSE = ...`) : cette clause n'accepte pas une variable de session en
DDL, donc le nom reste toujours écrit en clair. Convention : un warehouse
non-prod est préfixé `NP_` (ex. `NP_COMPUTE_WH`, partagé par DEV et QUA), le
même warehouse en prod n'a **pas** de préfixe (`COMPUTE_WH`). Le
déploiement ajoute ou retire ce préfixe `NP_` selon la cible — `QUA` ⇒
toujours `NP_<nom>`, `PRD` ⇒ toujours `<nom>` sans préfixe — quel que soit
l'état laissé dans le fichier par le développeur (le lint ne contraint
donc rien sur le nom du warehouse : n'importe quel nom, préfixé ou non, est
accepté, puisque le déploiement sait gérer les deux cas).

Chaque fichier `.sql` modifié par la PR est envoyé à Snowflake en une seule
requête (`cur.execute`), car les scripts sont des blocs anonymes
`DECLARE ... BEGIN ... END;` qui doivent être exécutés d'un bloc.

### Le fichier reflète ce qui a été déployé (`scripts/rewrite_deployed_state.py`)

Cette réécriture de `vEnv`/warehouse se fait **en mémoire**, uniquement
pour la requête envoyée à Snowflake — le fichier dans le dépôt, lui, ne
change jamais tout seul (il garde ce que le développeur a écrit, par
exemple `vEnv='DEV'`, même après un déploiement prod réussi).

Pour éviter la confusion ("pourquoi le fichier affiche encore `NP_` après
un déploiement prod ?"), le workflow appelant (`snowflake-git`) exécute, en
plus, `rewrite_deployed_state.py` juste après un déploiement réussi : ce
script applique la même réécriture (`vEnv`, warehouse) directement sur le
fichier sur disque, sans se connecter à Snowflake, puis le workflow
committe et pousse ce changement sur la branche de la PR. Le fichier finit
donc par afficher la valeur réellement déployée — mais seulement **après**
le run, et seulement l'état du **dernier** déploiement réussi sur cette
branche (si la même branche de ticket sert ensuite à une autre PR vers un
autre environnement, ce nouveau déploiement réécrit par-dessus).

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
