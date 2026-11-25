#!/usr/bin/env python3
"""Lint des scripts SQL Snowflake avant déploiement.

Vérifie les bonnes pratiques attendues par l'équipe Manitowoc :
  - pas de SELECT * (hors COUNT(*))
  - utilisation de variables de session pour l'environnement / la base cible :
    une seule base par environnement (DEV/QUA/PRD), pas de nom de base en
    dur (DEV/QUA/PRD utilisé tel quel ailleurs que dans la déclaration de
    vEnv) ; les zones (BRZ/SLV/GLD) sont des schémas à l'intérieur de cette
    base, pas des bases séparées
  - présence d'un bloc DECLARE ... vEnv ... pour piloter l'environnement
  - présence d'un en-tête de commentaire (Auteur / Date / Description)
  - pas de mot de passe ou de clé en clair dans le script
  - fichier rangé sous sql/<brz|slv|gld>/... (ordonnancement des
    dépendances entre scripts lors du déploiement, voir README section 5)
  - si le script déclare --DEPLOY_AFTER: <chemin>, ce chemin doit exister

Un script est lui-même déployé par la pipeline sur QUA ou PRD uniquement
(jamais DEV, promu manuellement par le développeur) — voir
deploy_snowflake.py — mais DEV reste un préfixe reconnu ici, car un script
venant de la branche dev porte encore souvent cette valeur au moment où il
est lu par le lint.

Usage: python lint_sql.py fichier1.sql fichier2.sql ...
Exit code != 0 si au moins un fichier contient une violation bloquante.
"""
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def log(message: str) -> None:
    """Log horodaté, pour pouvoir situer dans le temps une erreur dans les
    logs du job GitHub Actions (utile quand plusieurs fichiers sont lintés)."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {message}")


def annotate(violation: "Violation") -> None:
    """Émet une annotation GitHub Actions (::error::) pour que la violation
    s'affiche directement sur la ligne concernée dans l'onglet 'Files
    changed' de la PR, en plus du log texte habituel."""
    print(
        f"::error file={violation.file},line={violation.line},title=lint:{violation.rule}::{violation.message}"
    )


def write_step_summary(markdown: str) -> None:
    """Ajoute `markdown` au résumé du job (visible dans l'onglet Summary du
    run GitHub Actions). No-op si GITHUB_STEP_SUMMARY n'est pas défini
    (ex. exécution locale, hors CI)."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(markdown)

VALID_ZONES = {"brz", "slv", "gld"}

# Codes d'environnement valides pour vEnv / la base (une seule base par
# environnement : voir check_hardcoded_db). Indépendant de la convention de
# nommage des warehouses (préfixe NP_, voir deploy_snowflake.py), qui ne
# dépend pas de ces codes.
ALLOWED_ENVS = {"DEV", "QUA", "PRD"}

WAREHOUSE_PATTERN = re.compile(r"\bWAREHOUSE\s*=\s*([A-Za-z0-9_:]+)", re.IGNORECASE)


@dataclass
class Violation:
    file: str
    line: int
    rule: str
    message: str
    blocking: bool = True


def strip_comments(sql: str) -> str:
    """Retire les commentaires -- et /* */ pour éviter les faux positifs."""
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql


def check_select_star(raw: str, path: str) -> list[Violation]:
    violations = []
    for i, line in enumerate(raw.splitlines(), start=1):
        clean = strip_comments(line)
        if re.search(r"\bSELECT\s+\*", clean, re.IGNORECASE):
            if not re.search(r"COUNT\s*\(\s*\*\s*\)", clean, re.IGNORECASE):
                violations.append(
                    Violation(path, i, "no-select-star", "SELECT * interdit : lister explicitement les colonnes.")
                )
    return violations


def check_env_variable(sql_no_comments: str, path: str) -> list[Violation]:
    violations = []
    if not re.search(r"\bvEnv\b\s+varchar", sql_no_comments, re.IGNORECASE):
        violations.append(
            Violation(
                path, 1, "require-venv-variable",
                "Variable vEnv (DECLARE vEnv varchar := ...) absente : "
                "l'environnement cible doit être piloté par variable, pas codé en dur.",
            )
        )
    return violations


VENV_DECLARATION_PATTERN = re.compile(r"vEnv\s+varchar\s*:=", re.IGNORECASE)

STRING_LITERAL_PATTERN = re.compile(r"'[^']*'")


def mask_string_literals(line: str) -> str:
    """Remplace le contenu des chaînes '...' par des espaces, pour ne pas
    confondre du texte libre (ex. un commentaire COMMENT = '...truc DEV...')
    avec un identifiant utilisé en dur."""
    return STRING_LITERAL_PATTERN.sub(lambda m: " " * len(m.group(0)), line)


def check_hardcoded_db(sql_no_comments: str, path: str) -> list[Violation]:
    """Détecte un nom de base en dur : DEV/QUA/PRD utilisé tel quel comme
    identifiant (une seule base par environnement ; BRZ/SLV/GLD sont des
    schémas à l'intérieur, pas des bases séparées), sauf :
      - sur la ligne de déclaration de vEnv elle-même (la valeur initiale
        'DEV'/'QUA'/'PRD' y est un littéral de départ, pas un hardcode à
        corriger : le déploiement la réécrit de toute façon) ;
      - dans une chaîne de caractères '...' (texte libre, ex. un COMMENT),
        qui n'est pas un identifiant ;
      - dans une clause WAREHOUSE = ... (gérée séparément, voir
        check_warehouse_env_prefix, car on ne peut pas y mettre une
        variable de session en DDL)."""
    violations = []
    envs = "|".join(ALLOWED_ENVS)
    hardcoded_pattern = re.compile(rf"\b(?:{envs})\b")
    for i, line in enumerate(sql_no_comments.splitlines(), start=1):
        if VENV_DECLARATION_PATTERN.search(line):
            continue
        masked = mask_string_literals(line)
        warehouse_spans = [m.span(1) for m in WAREHOUSE_PATTERN.finditer(masked)]
        for m in hardcoded_pattern.finditer(masked):
            if any(start <= m.start() < end for start, end in warehouse_spans):
                continue
            violations.append(
                Violation(
                    path, i, "no-hardcoded-database",
                    f"Nom de base en dur détecté ('{m.group(0)}') : "
                    "utiliser la variable vEnv (ex. IDENTIFIER(:vEnv)) au lieu d'un littéral.",
                )
            )
    return violations


def check_header_comment(raw: str, path: str) -> list[Violation]:
    violations = []
    head = raw[:1500]
    has_header = bool(re.search(r"/\*.*?(Auteur|Date).*?\*/", head, re.IGNORECASE | re.DOTALL))
    if not has_header:
        violations.append(
            Violation(
                path, 1, "require-header-comment",
                "En-tête de commentaire manquant (bloc /* ... Auteur / Date / Description ... */).",
            )
        )
    return violations


def check_secrets(raw: str, path: str) -> list[Violation]:
    violations = []
    patterns = [
        (r"IDENTIFIED\s+BY\s*'[^']+'", "Mot de passe en clair détecté (IDENTIFIED BY)."),
        (r"-----BEGIN (RSA )?PRIVATE KEY-----", "Clé privée en clair détectée dans le script."),
    ]
    for pattern, message in patterns:
        for i, line in enumerate(raw.splitlines(), start=1):
            if re.search(pattern, line, re.IGNORECASE):
                violations.append(Violation(path, i, "no-secrets-in-sql", message))
    return violations


DEPLOY_AFTER_PATTERN = re.compile(r"--\s*DEPLOY_AFTER\s*:\s*(.+)", re.IGNORECASE)


def check_deploy_after(raw: str, path: str) -> list[Violation]:
    violations = []
    for i, line in enumerate(raw.splitlines(), start=1):
        m = DEPLOY_AFTER_PATTERN.search(line)
        if not m:
            continue
        for dep in [d.strip() for d in m.group(1).split(",") if d.strip()]:
            if dep == path:
                violations.append(
                    Violation(path, i, "no-self-dependency", "Un script ne peut pas déclarer DEPLOY_AFTER sur lui-même.")
                )
            elif not Path(dep).is_file():
                violations.append(
                    Violation(
                        path, i, "deploy-after-target-missing",
                        f"DEPLOY_AFTER référence '{dep}', introuvable dans le repo (chemin incorrect ?).",
                    )
                )
    return violations


def check_zone_folder(path: str) -> list[Violation]:
    parts = Path(path).parts
    violations = []
    if len(parts) < 3 or parts[0].lower() != "sql" or parts[1].lower() not in VALID_ZONES:
        violations.append(
            Violation(
                path, 1, "require-zone-folder",
                "Le script doit être rangé sous sql/brz/, sql/slv/ ou sql/gld/ "
                "pour que le déploiement respecte l'ordre des dépendances entre zones.",
            )
        )
    return violations


def lint_file(path: str) -> list[Violation]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        log(f"❌ Impossible de lire {path} : {exc.__class__.__name__}: {exc}")
        return [Violation(path, 1, "unreadable-file", f"Fichier illisible ({exc.__class__.__name__}: {exc}).")]

    clean = strip_comments(raw)
    violations: list[Violation] = []
    violations += check_select_star(raw, path)
    violations += check_env_variable(clean, path)
    violations += check_hardcoded_db(clean, path)
    violations += check_header_comment(raw, path)
    violations += check_secrets(raw, path)
    violations += check_zone_folder(path)
    violations += check_deploy_after(raw, path)

    if violations:
        log(f"❌ {path} : {len(violations)} violation(s)")
    else:
        log(f"✅ {path} : OK")
    return violations


def main(argv: list[str]) -> int:
    files = [f for f in argv if f.strip()]
    if not files:
        log("Aucun fichier SQL à analyser.")
        return 0

    log(f"Lint de {len(files)} fichier(s) : {', '.join(files)}")

    all_violations: list[Violation] = []
    for file_path in files:
        all_violations += lint_file(file_path)

    if not all_violations:
        log(f"✅ Lint OK sur {len(files)} fichier(s).")
        write_step_summary(
            f"### Lint SQL\n\n✅ {len(files)} fichier(s) analysé(s), aucune violation.\n\n"
            + "\n".join(f"- `{f}`" for f in files) + "\n"
        )
        return 0

    log(f"❌ {len(all_violations)} violation(s) détectée(s) sur {len({v.file for v in all_violations})} fichier(s) :")
    for v in all_violations:
        log(f"  ERREUR [{v.rule}] {v.file}:{v.line} → {v.message}")
        annotate(v)

    summary_rows = "\n".join(
        f"| `{v.file}` | {v.line} | `{v.rule}` | {v.message} |" for v in all_violations
    )
    write_step_summary(
        "### Lint SQL\n\n"
        f"❌ {len(all_violations)} violation(s) sur {len(files)} fichier(s) analysé(s).\n\n"
        "| Fichier | Ligne | Règle | Cause |\n"
        "|---|---|---|---|\n"
        f"{summary_rows}\n"
    )

    blocking = [v for v in all_violations if v.blocking]
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
