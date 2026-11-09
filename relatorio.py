"""
relatorio.py - junta as saídas do inventario_ifc.py num resultado.json e gera o
relatorio.html (o visualizador com os dados embutidos, abre sem servidor).

Ideia e concepção: André Brum Rivoire.

Uso (depois de rodar o inventário):
  python relatorio.py --config config_local.json
O inventario_ifc.py chama este módulo sozinho no fim da etapa de resumo.
"""
import argparse
import csv
import datetime
import json
import os
import re

AQUI = os.path.dirname(os.path.abspath(__file__))


def _le_csv(caminho):
    if not os.path.exists(caminho):
        return []
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def monta_resultado(saida, disciplinas, categorias_json):
    """disciplinas: lista de {codigo, nome} na ordem das colunas."""
    from inventario_ifc import Categorias
    cat = Categorias(categorias_json)
    rot2chave = {v: k for k, v in cat.rotulos.items()}

    manifesto = _le_csv(os.path.join(saida, "manifesto.csv"))
    n_mod = {}
    for m in manifesto:
        n_mod[m["disciplina"]] = n_mod.get(m["disciplina"], 0) + 1
    D = [d["codigo"] for d in disciplinas]

    entidades = []
    for r in _le_csv(os.path.join(saida, "entidades_por_disciplina.csv")):
        n = {}
        for k, v in r.items():
            m = re.match(r"^([A-Z]+) \(\d+\)$", k or "")
            if m:
                n[m.group(1)] = _int(v)
        nome = r["entidade"]
        entidades.append({"nome": nome, "grupo": rot2chave.get(r["grupo"], cat.grupo(nome)),
                          "construido": cat.construido(nome), "n": n})

    classificacao = [{
        "modelo": r["modelo"], "disciplina": r["disciplina"],
        "relacoes": _int(r["relacoes_de_classificacao"]), "elementos": _int(r["elementos_ifccsv"]),
        "classificados": _int(r["elementos_classificados"]), "sistemas": r["sistemas_declarados"],
        "psets_padrao": _int(r["psets_nome_Pset_"]), "psets_outros": _int(r["psets_outros_nomes"]),
    } for r in _le_csv(os.path.join(saida, "classificacao_por_modelo.csv"))]

    quantidades = [{"disciplina": r["disciplina"], "nome": r["nome_do_conjunto"], "n": _int(r["n"]),
                    "modelos": _int(r["modelos"])}
                   for r in _le_csv(os.path.join(saida, "conjuntos_quantidades_por_disciplina.csv"))]

    divergencias = [{"modelo": r["modelo"], "disciplina": r["disciplina"], "entidade": r["entidade"],
                     "n1": _int(r["n_metodo_1"]), "n2": _int(r["n_ifcopenshell"])}
                    for r in _le_csv(os.path.join(saida, "conferencia_divergencias.csv"))]

    modelos = [{"codigo": m["modelo"], "disciplina": m["disciplina"], "esquema": m["esquema"],
                "tamanho": _int(m["tamanho_bytes"]), "sha256": m["sha256"]} for m in manifesto]

    conferidos = set()
    for r in _le_csv(os.path.join(saida, "entidades_por_modelo.csv")):
        if r.get("n_ifcopenshell", "") != "":
            conferidos.add(r["modelo"])

    versoes = {}
    p = os.path.join(saida, "versoes.json")
    if os.path.exists(p):
        versoes = json.load(open(p, encoding="utf-8"))

    return {
        "ferramenta": "inventario_ifc",
        "gerado_em": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "versoes": versoes,
        "disciplinas": [{"codigo": d["codigo"], "nome": d.get("nome", d["codigo"]), "modelos": n_mod.get(d["codigo"], 0)}
                        for d in disciplinas],
        "grupos": [{"chave": g, "rotulo": cat.rotulos[g]} for g in cat.ordem],
        "modelos": modelos,
        "modelos_conferidos": len(conferidos),
        "entidades": entidades,
        "classificacao": classificacao,
        "quantidades": quantidades,
        "divergencias": divergencias,
    }


def gera(saida, disciplinas, categorias_json=None):
    categorias_json = categorias_json or os.path.join(AQUI, "categorias.json")
    res = monta_resultado(saida, disciplinas, categorias_json)
    with open(os.path.join(saida, "resultado.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    modelo = open(os.path.join(AQUI, "visualizador.html"), encoding="utf-8").read()
    marca = '<script id="dados" type="application/json">null</script>'
    assert marca in modelo, "marcador de dados não encontrado no visualizador.html"
    dados = json.dumps(res, ensure_ascii=False).replace("</", "<\\/")
    html = modelo.replace(marca, '<script id="dados" type="application/json">' + dados + "</script>")
    # as regras de agrupamento do HTML passam a ser as do categorias.json em uso
    regras = json.load(open(categorias_json, encoding="utf-8"))
    regras = json.dumps(regras, ensure_ascii=False).replace("</", "<\\/")
    html, n = re.subn(r'<script id="categorias" type="application/json">.*?</script>',
                      lambda m: '<script id="categorias" type="application/json">' + regras + "</script>", html, count=1, flags=re.S)
    assert n == 1, "bloco de categorias não encontrado no visualizador.html"
    with open(os.path.join(saida, "relatorio.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.join(saida, "relatorio.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gera resultado.json e relatorio.html a partir das saídas do inventário.")
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    cfg = json.load(open(a.config, encoding="utf-8"))
    print(gera(cfg["saida"], cfg["disciplinas"], cfg.get("categorias")))
