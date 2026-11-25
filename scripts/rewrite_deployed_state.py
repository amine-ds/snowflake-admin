#!/usr/bin/env python3
"""Réécrit sur disque, pour chaque fichier donné, l'état exact qui vient
d'être déployé (vEnv + warehouse réécrits pour TARGET_ENV) — sans se
connecter à Snowflake. Appelé par le workflow juste après un déploiement
réussi, pour que le fichier dans le dépôt reflète ce qui a réellement été
envoyé à Snowflake (sinon le fichier garde pour toujours la valeur écrite
par le développeur, ex. vEnv='DEV' / WAREHOUSE = NP_COMPUTE_WH, même après
un déploiement prod réussi — ce qui prête à confusion).

Usage: python rewrite_deployed_state.py fichier1.sql fichier2.sql ...
Affiche sur stdout, un par ligne, le chemin de chaque fichier réellement
modifié (pour que le workflow sache quoi `git add`).
"""
import os
import sys

from deploy_snowflake import ALLOWED_ENVS, force_target_env, force_target_warehouse_env


def main(argv: list[str]) -> int:
    target_env = os.environ.get("TARGET_ENV", "")
    if target_env not in ALLOWED_ENVS:
        print(f"TARGET_ENV invalide ou absent : '{target_env}' (attendu QUA ou PRD).", file=sys.stderr)
        return 1

    for file_path in argv:
        if not file_path.strip():
            continue
        with open(file_path, encoding="utf-8") as fh:
            sql = fh.read()

        new_sql = force_target_warehouse_env(force_target_env(sql, target_env), target_env)

        if new_sql != sql:
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(new_sql)
            print(file_path)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
