"""Paridade dos códigos de aviso do Importar/Exportar.

Três pontas têm de andar juntas, e este teste quebra quando uma delas sai do passo:

  1. o que o motor EMITE (cada ``env.warn(..., "codigo")`` no código-fonte de ``portability/``);
  2. a lista oficial ``portability/warnings.py``;
  3. a frase que o usuário lê, em ``web/static/js/utils/portabilityLogic.js``.

Sem isto um código novo chega à tela como "Aviso: nome_interno", e uma frase de código que ninguém mais
emite fica mentindo para quem lê o JS.

Roda direto (``python tests/test_portability_warnings.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from portability.warnings import NOTE_CODES, WARNING_CODES  # noqa: E402

_PKG = _ROOT / "portability"
_JS = _ROOT / "web" / "static" / "js" / "utils" / "portabilityLogic.js"

def _emitted() -> dict[str, list[str]]:
    """``{código: [arquivo:linha, ...]}`` de toda chamada ``<x>.warn(entidade, chave, "código")``.

    Lê a AST (não regex): pega a chamada em qualquer formatação e distingue o 3º argumento literal.
    Um código que não é literal (variável, expressão) é reportado à parte — não dá para conferir."""
    found: dict[str, list[str]] = {}
    dynamic: list[str] = []
    for path in sorted(_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "warn" and len(node.args) >= 3):
                continue
            code = node.args[2]
            where = f"{path.name}:{node.lineno}"
            if isinstance(code, ast.Constant) and isinstance(code.value, str):
                found.setdefault(code.value, []).append(where)
            elif isinstance(code, ast.IfExp):                       # "a" if cond else "b"
                for branch in (code.body, code.orelse):
                    if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                        found.setdefault(branch.value, []).append(where)
                    else:
                        dynamic.append(where)
            else:
                dynamic.append(where)
    found["__dynamic__"] = dynamic
    return found

def _js_object(name: str) -> dict[str, str]:
    """Chaves e frases de ``export const NAME = { chave: 'frase', ... };`` do JS."""
    src = _JS.read_text(encoding="utf-8")
    m = re.search(rf"export const {name} = \{{(.*?)\n\}};", src, re.S)
    assert m, f"{name} não encontrado em portabilityLogic.js"
    out: dict[str, str] = {}
    for key, text in re.findall(r"^\s{2}([a-z_]+):\s*'((?:[^'\\]|\\.)*)',?\s*$", m.group(1), re.M):
        out[key] = text
    return out

def test_the_engine_only_emits_listed_codes():
    emitted = _emitted()
    assert emitted.pop("__dynamic__") == [], "código de aviso não literal: o teste não consegue conferi-lo"
    unknown = {c: w for c, w in emitted.items() if c not in WARNING_CODES}
    assert unknown == {}, f"código emitido que não está em portability/warnings.py: {unknown}"

def test_every_listed_code_is_still_emitted_by_something():
    emitted = _emitted()
    emitted.pop("__dynamic__")
    dead = sorted(set(WARNING_CODES) - set(emitted))
    assert dead == [], f"código listado que nenhuma chamada emite mais: {dead}"

def test_the_server_never_sends_free_text_as_a_code():
    for code in list(WARNING_CODES) + list(NOTE_CODES):
        assert re.fullmatch(r"[a-z][a-z0-9_]{2,40}", code), code

def test_every_code_has_a_sentence_on_screen():
    labels = _js_object("WARNING_LABELS")
    missing = sorted(set(WARNING_CODES) - set(labels))
    assert missing == [], f"código sem frase em portabilityLogic.js (o usuário veria 'Aviso: …'): {missing}"

def test_the_screen_has_no_sentence_for_a_code_the_engine_does_not_know():
    labels = _js_object("WARNING_LABELS")
    stale = sorted(set(labels) - set(WARNING_CODES))
    assert stale == [], f"frase para código que o motor não emite: {stale}"

def test_every_note_has_a_sentence_on_screen_and_the_engine_emits_it():
    labels = _js_object("NOTE_LABELS")
    assert set(labels) == set(NOTE_CODES)
    src = (_PKG / "handlers.py").read_text(encoding="utf-8")
    for note in NOTE_CODES:
        assert f'notes.append("{note}")' in src, f"a nota {note} não é emitida"

def test_sentences_are_not_empty_and_carry_no_placeholder():
    for name in ("WARNING_LABELS", "NOTE_LABELS"):
        for code, text in _js_object(name).items():
            assert len(text) > 15, (name, code)
            assert "${" not in text and "{{" not in text, (name, code)

def test_the_js_parser_really_reads_the_labels():
    """Se o regex do teste parasse de casar, os testes acima passariam em vazio."""
    assert len(_js_object("WARNING_LABELS")) == len(WARNING_CODES)
    assert len(_js_object("NOTE_LABELS")) == len(NOTE_CODES) == 2

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── ajuda do sistema e navegação do painel ──────────────────────────────────

_PANEL = _ROOT / "web" / "static" / "js" / "components" / "ConfigPanel.js"
_HELP = _ROOT / "agent" / "SYSTEM_HELP.md"
_ANCHOR = "import-export"

def test_the_help_link_points_to_an_anchor_the_panel_really_has():
    """O link da ajuda, a regra do chat, o mapa de abas e o `id` do card têm de concordar."""
    help_text = _HELP.read_text(encoding="utf-8")
    assert f"{{{{base_url}}}}/painel?aba=sistema#{_ANCHOR}" in help_text
    panel = _PANEL.read_text(encoding="utf-8")
    assert f"'{_ANCHOR}': 'system'" in panel, "PANEL_TARGET_TABS não leva a âncora para a aba Sistema"
    assert f'id="{_ANCHOR}"' in panel, "o card não tem o id da âncora"
    assert "PortabilitySettings" in panel
    from server.routes.chat import _HELP_LINK_RULES
    targets = [path for _rx, path, _label in _HELP_LINK_RULES]
    assert f"/painel?aba=sistema#{_ANCHOR}" in targets

def test_the_import_export_rule_wins_over_the_generic_ones_it_overlaps():
    """"backup dos dados" cairia em ``banco|dados`` e "importar a chave" em ``chave|api`` se a regra
    nova viesse depois; a ordem é parte do contrato."""
    from server.routes.chat import _HELP_LINK_RULES
    paths = [p for _rx, p, _l in _HELP_LINK_RULES]
    ie = paths.index(f"/painel?aba=sistema#{_ANCHOR}")
    for other in ("/painel?aba=sistema#password", "/painel?aba=sistema#database",
                  "/painel?aba=modelos-midia#api-key", "/painel?aba=sistema#update"):
        assert ie < paths.index(other), other
    pick = lambda q: next(p for rx, p, _l in _HELP_LINK_RULES if rx.search(q))
    assert pick("fazer backup dos dados") == f"/painel?aba=sistema#{_ANCHOR}"
    assert pick("exportar a chave de api") == f"/painel?aba=sistema#{_ANCHOR}"
    assert pick("trocar a senha do painel") == "/painel?aba=sistema#password"

def test_the_help_text_names_what_the_screen_really_calls_things():
    help_text = _HELP.read_text(encoding="utf-8")
    section = help_text.split("(importar e exportar)", 1)[1].split("### Configurar ou migrar", 1)[0]
    js = _JS.read_text(encoding="utf-8") + "".join(
        p.read_text(encoding="utf-8") for p in (_ROOT / "web/static/js/components/portability").glob("*.js"))
    for label in ("Gerar pacote", "Baixar pacote", "Enviar e verificar", "Recalcular prévia",
                  "Confirmar importação", "Revisei e quero iniciar", "Incluir segredos",
                  "Se já existir neste servidor", "Operações recentes"):
        assert label in section, f"a ajuda não cita '{label}'"
        assert label in js, f"a tela não tem mais '{label}': corrija a ajuda"
