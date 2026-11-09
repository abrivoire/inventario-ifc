"""
inventario_ifc.py - inventário reprodutível das entidades IFC de um conjunto de modelos

Ideia e concepção: André Brum Rivoire.

Para cada arquivo IFC das pastas listadas no config, a ferramenta faz:

  1. TEXTO: lê o arquivo STEP como texto, conta cada entidade pelo nome escrito
     entre o primeiro '=' e o primeiro '(' de cada linha de instância (tolera o
     espaço opcional depois do '='), lê o esquema do cabeçalho e calcula o
     SHA-256 do arquivo (manifesto).
  2. IFCOPENSHELL: abre o modelo com o IfcOpenShell (leitura pelo esquema, sem
     texto) e conta cada entidade não abstrata com by_type(nome,
     include_subtypes=False); lê os sistemas de classificação, os nomes dos
     conjuntos de quantidades e de propriedades; e exporta todos os IfcElement
     com o IfcCSV (classe e classificação de cada elemento).
  3. RESUMO: junta tudo por disciplina, compara as contagens dos dois métodos e
     grava os CSV de resultado.

Os modelos aparecem nas saídas com um código anônimo (ARQ-01, INT-01, ...),
atribuído pela ordem alfabética do nome do arquivo dentro de cada pasta. A
tabela que liga código e nome real vai para 'mapa_local', fora das saídas.

Uso:
  python inventario_ifc.py --config config_local.json
  python inventario_ifc.py --config config_local.json --etapas texto,resumo
"""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

AQUI = os.path.dirname(os.path.abspath(__file__))
RE_SCHEMA = re.compile(rb"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'")
LIMITE_GRANDE = 300 * 1024 * 1024  # arquivos acima disso abrem um de cada vez no IfcOpenShell


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------
def grava_csv(caminho, cabecalho, linhas):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cabecalho)
        w.writerows(linhas)


def le_json(caminho, padrao=None):
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    return padrao


def grava_json(caminho, dado):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dado, f, ensure_ascii=False, indent=1)
    os.replace(tmp, caminho)


def pct(a, b):
    return "%.1f" % (100.0 * a / b) if b else ""


class Categorias:
    def __init__(self, caminho):
        c = le_json(caminho)
        self.rotulos = c["rotulos"]
        self.ordem = c["ordem_dos_grupos"]
        self.excecoes = c["excecoes"]
        self.prefixo_rel = c["relacao_prefixo"]
        self.elemento = set(c["elemento"])
        self.re_prop = re.compile(c["propriedade"])
        self.re_mat = re.compile(c["material"])
        self.re_ctx = re.compile(c["contexto"])
        ec = c["elementos_construidos"]
        self.ec_sufixos = tuple(ec["excluir_sufixos"])
        self.ec_excluir = set(ec["excluir"])

    def grupo(self, nome):
        if nome in self.excecoes:
            return self.excecoes[nome]
        if nome.startswith(self.prefixo_rel):
            return "relacao"
        if nome in self.elemento:
            return "elemento"
        if self.re_prop.match(nome):
            return "propriedade"
        if self.re_mat.match(nome):
            return "material"
        if self.re_ctx.match(nome):
            return "contexto"
        return "geometria"

    def construido(self, nome):
        return (self.grupo(nome) == "elemento" and not nome.endswith(self.ec_sufixos)
                and nome not in self.ec_excluir)


# ----------------------------------------------------------------------------
# etapa 1: texto + manifesto
# ----------------------------------------------------------------------------
def conta_texto(caminho):
    c = Counter()
    schema = None
    h = hashlib.sha256()
    resto = b""
    with open(caminho, "rb") as f:
        while True:
            bloco = f.read(16 * 1024 * 1024)
            if not bloco:
                break
            h.update(bloco)
            bloco = resto + bloco
            linhas = bloco.split(b"\n")
            resto = linhas.pop()
            for linha in linhas:
                if not linha.startswith(b"#"):
                    if schema is None and b"FILE_SCHEMA" in linha:
                        m = RE_SCHEMA.search(linha)
                        if m:
                            schema = m.group(1).decode("ascii", "replace")
                    continue
                eq = linha.find(b"=")
                par = linha.find(b"(")
                if eq < 0 or par < 0 or par < eq:
                    continue
                c[linha[eq + 1:par].strip().decode("ascii", "replace").upper()] += 1
    if resto.startswith(b"#"):
        eq, par = resto.find(b"="), resto.find(b"(")
        if 0 <= eq < par:
            c[resto[eq + 1:par].strip().decode("ascii", "replace").upper()] += 1
    return {"schema": schema, "sha256": h.hexdigest(), "contagem": dict(c)}


# ----------------------------------------------------------------------------
# etapa 2: IfcOpenShell + IfcCSV (roda em subprocesso, um arquivo por vez)
# ----------------------------------------------------------------------------
def worker_ifcopenshell(caminho, saida_csv):
    import ifcopenshell
    import ifcopenshell.ifcopenshell_wrapper as W
    import ifcopenshell.util.selector as sel
    import ifccsv

    m = ifcopenshell.open(caminho)
    esquema = W.schema_by_name(m.schema)
    cont = {}
    for d in esquema.declarations():
        if isinstance(d, W.entity) and not d.is_abstract():
            n = len(m.by_type(d.name(), include_subtypes=False))
            if n:
                cont[d.name().upper()] = n

    sistemas = []
    for c in m.by_type("IfcClassification"):
        sistemas.append({"nome": c.Name, "fonte": c.Source, "edicao": c.Edition})
    qto = Counter(q.Name or "(sem nome)" for q in m.by_type("IfcElementQuantity"))
    pset = Counter("Pset_" if (p.Name or "").startswith("Pset_") else "outro" for p in m.by_type("IfcPropertySet"))

    import ifcopenshell.util.classification as ucl

    elementos = sel.filter_elements(m, "IfcElement")
    # o código da referência mudou de nome entre os esquemas:
    # IFC2X3 IfcClassificationReference.ItemReference -> IFC4 .Identification
    cod = "classification.ItemReference" if m.schema == "IFC2X3" else "classification.Identification"
    exp = ifccsv.IfcCsv()
    exp.export(m, elementos, ["class", cod, "classification.Name"],
               output=saida_csv, format="csv", delimiter=";", null="-")
    # elementos por classe contados nas linhas exportadas pelo IfcCSV (conferidos depois com o by_type)
    por_classe = Counter()
    with open(saida_csv, encoding="utf-8") as f:
        r = csv.reader(f, delimiter=";")
        ic = next(r).index("class")
        for linha in r:
            por_classe[linha[ic].upper()] += 1
    # classificado = o IfcOpenShell encontra ao menos uma referência de classificação
    # para o elemento (direta ou herdada do tipo); não depende do texto do CSV
    classificados = Counter(e.is_a().upper() for e in elementos if ucl.get_references(e))
    return {"schema": m.schema, "contagem": cont, "sistemas_classificacao": sistemas,
            "conjuntos_quantidades": dict(qto), "conjuntos_propriedades": dict(pset),
            "ifccsv_por_classe": dict(por_classe), "ifccsv_classificados": dict(classificados)}


def roda_ifcopenshell(modelos, saida, estado):
    pasta_csv = os.path.join(saida, "ifccsv")
    os.makedirs(pasta_csv, exist_ok=True)
    pendentes = [m for m in modelos if m["codigo"] not in estado]

    def um(m):
        t0 = time.time()
        destino = os.path.join(pasta_csv, m["codigo"] + ".csv")
        p = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "--worker", m["caminho"], destino],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            print("  ERRO %s: %s" % (m["codigo"], p.stderr.strip()[-300:]), flush=True)
            return m["codigo"], None
        r = json.loads(p.stdout.strip().splitlines()[-1])
        r["segundos"] = round(time.time() - t0)
        print("  %-7s %5d s  %d tipos" % (m["codigo"], r["segundos"], len(r["contagem"])), flush=True)
        return m["codigo"], r

    pequenos = [m for m in pendentes if m["tamanho"] < LIMITE_GRANDE]
    grandes = [m for m in pendentes if m["tamanho"] >= LIMITE_GRANDE]
    with ThreadPoolExecutor(max_workers=2) as ex:
        for cod, r in ex.map(um, pequenos):
            if r:
                estado[cod] = r
                grava_json(os.path.join(saida, "_estado_ifcopenshell.json"), estado)
    for m in grandes:
        cod, r = um(m)
        if r:
            estado[cod] = r
            grava_json(os.path.join(saida, "_estado_ifcopenshell.json"), estado)
    return estado


# ----------------------------------------------------------------------------
# etapa 3: resumo
# ----------------------------------------------------------------------------
def resumo(modelos, disciplinas, saida, cat, texto, ios):
    D = [d["codigo"] for d in disciplinas]
    n_mod = Counter(m["disciplina"] for m in modelos)

    # manifesto
    grava_csv(os.path.join(saida, "manifesto.csv"),
              ["modelo", "disciplina", "esquema", "tamanho_bytes", "sha256"],
              [[m["codigo"], m["disciplina"], texto[m["codigo"]]["schema"], m["tamanho"], texto[m["codigo"]]["sha256"]]
               for m in modelos])

    # contagem por modelo, nos dois métodos, e conferência
    longo, diverg = [], []
    for m in modelos:
        t = texto[m["codigo"]]["contagem"]
        o = ios.get(m["codigo"], {}).get("contagem")
        for e in sorted(set(t) | set(o or {})):
            nt, no = t.get(e, 0), (o.get(e, 0) if o is not None else "")
            longo.append([m["codigo"], m["disciplina"], e, cat.grupo(e), nt, no])
            if o is not None and nt != no:
                diverg.append([m["codigo"], m["disciplina"], e, nt, no])
        ic = ios.get(m["codigo"], {}).get("ifccsv_por_classe")
        if ic is not None and o is not None:
            for e, n in ic.items():
                if n != o.get(e, 0):
                    diverg.append([m["codigo"], m["disciplina"], e + " (IfcCSV)", n, o.get(e, 0)])
    grava_csv(os.path.join(saida, "entidades_por_modelo.csv"),
              ["modelo", "disciplina", "entidade", "grupo", "n_texto", "n_ifcopenshell"], longo)
    grava_csv(os.path.join(saida, "conferencia_divergencias.csv"),
              ["modelo", "disciplina", "entidade", "n_metodo_1", "n_ifcopenshell"], diverg)

    # entidades por disciplina (contagem por texto, conferida acima)
    T = {d: Counter() for d in D}
    for m in modelos:
        T[m["disciplina"]].update(texto[m["codigo"]]["contagem"])
    nomes = sorted(set().union(*T.values()), key=lambda n: (cat.ordem.index(cat.grupo(n)),
                                                             -sum(T[d][n] for d in D), n))
    grava_csv(os.path.join(saida, "entidades_por_disciplina.csv"),
              ["entidade", "grupo"] + ["%s (%d)" % (d, n_mod[d]) for d in D] + ["total"],
              [[n, cat.rotulos[cat.grupo(n)]] + [T[d][n] for d in D] + [sum(T[d][n] for d in D)] for n in nomes])

    # parcela de cada grupo
    tot = {d: sum(T[d].values()) for d in D}
    TT = sum(tot.values())
    linhas = []
    for g in cat.ordem:
        v = {d: sum(T[d][n] for n in nomes if cat.grupo(n) == g) for d in D}
        linhas.append([cat.rotulos[g]] + [x for d in D for x in (v[d], pct(v[d], tot[d]))]
                      + [sum(v.values()), pct(sum(v.values()), TT)])
    linhas.append(["Total"] + [x for d in D for x in (tot[d], "100.0")] + [TT, "100.0"])
    grava_csv(os.path.join(saida, "grupos_por_disciplina.csv"),
              ["grupo"] + [x for d in D for x in (d + "_n", d + "_%")] + ["total_n", "total_%"], linhas)

    # elementos construídos
    ec = sorted([n for n in nomes if cat.construido(n)], key=lambda n: -sum(T[d][n] for d in D))
    tot_ec = {d: sum(T[d][n] for n in ec) for d in D}
    linhas = [[n] + [T[d][n] for d in D] + [sum(T[d][n] for d in D)] for n in ec]
    linhas.append(["TOTAL"] + [tot_ec[d] for d in D] + [sum(tot_ec.values())])
    px = "IFCBUILDINGELEMENTPROXY"
    linhas.append(["% IFCBUILDINGELEMENTPROXY"] + [pct(T[d][px], tot_ec[d]) for d in D]
                  + [pct(sum(T[d][px] for d in D), sum(tot_ec.values()))])
    grava_csv(os.path.join(saida, "elementos_construidos.csv"), ["entidade"] + D + ["total"], linhas)

    # classificação, quantidades e propriedades por modelo (IfcOpenShell e IfcCSV)
    lin_cl, lin_q = [], []
    q_disc = defaultdict(Counter)
    q_mod = defaultdict(Counter)
    for m in modelos:
        r = ios.get(m["codigo"])
        t = texto[m["codigo"]]["contagem"]
        if not r:
            continue
        sist = " | ".join(sorted({(s["nome"] or "") for s in r["sistemas_classificacao"]}))
        n_el = sum(r["ifccsv_por_classe"].values())
        n_cl = sum(r["ifccsv_classificados"].values())
        lin_cl.append([m["codigo"], m["disciplina"], t.get("IFCRELASSOCIATESCLASSIFICATION", 0), n_el, n_cl,
                       pct(n_cl, n_el), sist, r["conjuntos_propriedades"].get("Pset_", 0),
                       r["conjuntos_propriedades"].get("outro", 0)])
        for nome, n in r["conjuntos_quantidades"].items():
            q_disc[m["disciplina"]][nome] += n
            q_mod[m["disciplina"]][nome] += 1
            lin_q.append([m["codigo"], m["disciplina"], nome, n])
    grava_csv(os.path.join(saida, "classificacao_por_modelo.csv"),
              ["modelo", "disciplina", "relacoes_de_classificacao", "elementos_ifccsv", "elementos_classificados",
               "%_classificados", "sistemas_declarados", "psets_nome_Pset_", "psets_outros_nomes"], lin_cl)
    grava_csv(os.path.join(saida, "conjuntos_quantidades_por_modelo.csv"),
              ["modelo", "disciplina", "nome_do_conjunto", "n"], lin_q)
    grava_csv(os.path.join(saida, "conjuntos_quantidades_por_disciplina.csv"),
              ["disciplina", "nome_do_conjunto", "n", "modelos"],
              [[d, nome, n, q_mod[d][nome]] for d in D for nome, n in q_disc[d].most_common()])

    print("\nRESUMO")
    print("  modelos: %s" % ", ".join("%s %d" % (d, n_mod[d]) for d in D))
    print("  tipos de entidade: %d | instâncias: %d" % (len(nomes), TT))
    print("  conferidos no IfcOpenShell: %d de %d | divergências: %d" % (len(ios), len(modelos), len(diverg)))


# ----------------------------------------------------------------------------
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        # ensure_ascii=True: a saída do subprocesso não depende da página de código do console
        print(json.dumps(worker_ifcopenshell(sys.argv[2], sys.argv[3]), ensure_ascii=True))
        return

    ap = argparse.ArgumentParser(description="Inventário reprodutível de entidades IFC.")
    ap.add_argument("--config", required=True)
    ap.add_argument("--etapas", default="texto,ifcopenshell,resumo")
    a = ap.parse_args()
    cfg = le_json(a.config)
    etapas = set(a.etapas.split(","))
    saida = cfg["saida"]
    cat = Categorias(cfg.get("categorias", os.path.join(AQUI, "categorias.json")))

    # lista os modelos e atribui os códigos anônimos
    modelos = []
    for d in cfg["disciplinas"]:
        arqs = sorted(n for n in os.listdir(d["pasta"]) if n.lower().endswith(".ifc"))
        for i, n in enumerate(arqs, 1):
            c = os.path.join(d["pasta"], n)
            modelos.append({"codigo": "%s-%02d" % (d["codigo"], i), "disciplina": d["codigo"],
                            "arquivo": n, "caminho": c, "tamanho": os.path.getsize(c)})
    grava_csv(cfg["mapa_local"], ["modelo", "disciplina", "arquivo"],
              [[m["codigo"], m["disciplina"], m["arquivo"]] for m in modelos])
    print("%d modelos; mapa código -> arquivo em %s" % (len(modelos), cfg["mapa_local"]))

    arq_texto = os.path.join(saida, "_estado_texto.json")
    texto = le_json(arq_texto, {})
    if "texto" in etapas:
        pend = [m for m in modelos if m["codigo"] not in texto]
        print("TEXTO: %d arquivos" % len(pend))
        with ThreadPoolExecutor(max_workers=4) as ex:
            for m, r in zip(pend, ex.map(lambda m: conta_texto(m["caminho"]), pend)):
                texto[m["codigo"]] = r
                print("  %-7s %s %d tipos" % (m["codigo"], r["schema"], len(r["contagem"])), flush=True)
        grava_json(arq_texto, texto)

    arq_ios = os.path.join(saida, "_estado_ifcopenshell.json")
    ios = le_json(arq_ios, {})
    if "ifcopenshell" in etapas:
        print("IFCOPENSHELL + IFCCSV: %d arquivos" % sum(1 for m in modelos if m["codigo"] not in ios))
        ios = roda_ifcopenshell(modelos, saida, ios)

    if "resumo" in etapas:
        faltam = [m["codigo"] for m in modelos if m["codigo"] not in texto]
        if faltam:
            sys.exit("Faltam contagens por texto: %s" % faltam)
        resumo(modelos, cfg["disciplinas"], saida, cat, texto, ios)
        import ifcopenshell
        versoes = {"python": sys.version.split()[0], "ifcopenshell": ifcopenshell.version}
        try:
            from importlib.metadata import version
            versoes["ifccsv"] = version("ifccsv")
        except Exception:
            pass
        grava_json(os.path.join(saida, "versoes.json"), versoes)
        # resultado.json + relatorio.html (visualizador com os dados embutidos)
        sys.path.insert(0, AQUI)  # com python -I a pasta do script não entra no sys.path
        import relatorio
        print("  relatório: %s" % relatorio.gera(saida, cfg["disciplinas"], cfg.get("categorias")))


if __name__ == "__main__":
    main()
