# inventario_ifc

Inventário reprodutível das entidades de um conjunto de modelos IFC, separado por disciplina.

Ideia e concepção: André Brum Rivoire.

A ferramenta foi feita numa pesquisa de mestrado sobre requisitos de informação e IDS (Information Delivery Specification) para conhecer o que os programas de projeto exportam de fato nos arquivos IFC antes de escrever os requisitos. Ela responde a perguntas como estas:

- quais entidades IFC aparecem nos modelos de cada disciplina e quantas vezes;
- que parte das instâncias é geometria e que parte é classificação, propriedades, quantidades e material;
- quantos elementos construídos há de cada classe e quantos são genéricos (`IfcBuildingElementProxy`);
- que sistemas de classificação os modelos declaram e quantos elementos estão classificados;
- com que nomes os conjuntos de quantidades (`IfcElementQuantity`) são gravados.

## Como funciona

Cada arquivo passa por dois métodos independentes, e o resultado de um confere o outro.

1. **Texto.** O arquivo STEP é lido como texto. Em cada linha de instância (`#123=IFCWALL(...);`) o nome da entidade é o que está entre o primeiro `=` e o primeiro `(`, sem os espaços das pontas. Isso tolera o espaço opcional que alguns exportadores gravam depois do `=` (`#123= IFCWALL(...)`). Nessa mesma leitura saem o esquema do cabeçalho e o SHA-256 do arquivo.
2. **IfcOpenShell.** O modelo é aberto com o [IfcOpenShell](https://ifcopenshell.org/), que lê pelo esquema e não pelo texto. Cada entidade não abstrata do esquema é contada com `model.by_type(nome, include_subtypes=False)`. Nessa etapa também se leem os sistemas de classificação (`IfcClassification`), os nomes dos conjuntos de quantidades e de propriedades, e todos os `IfcElement` são exportados com o [IfcCSV](https://docs.ifcopenshell.org/) com a classe e a classificação de cada um.

Depois a etapa de resumo junta tudo por disciplina, compara as contagens (texto, IfcOpenShell e IfcCSV) e grava os resultados em CSV.

O agrupamento das entidades (elementos, propriedades e quantidades, materiais e classificação, relações, contexto, geometria) e a definição de elemento construído estão em [`categorias.json`](categorias.json), separados do código para poderem ser lidos e alterados.

## Visualizador no navegador (sem instalar nada)

O arquivo [`visualizador.html`](visualizador.html) abre com dois cliques em qualquer navegador moderno e serve para duas coisas:

1. **Abrir modelos IFC direto no navegador.** Arraste os arquivos .ifc para a caixa de cada disciplina (ou abra uma pasta com uma subpasta por disciplina) e clique em *Analisar*. A página conta as entidades pelo mesmo método de texto descrito abaixo, lendo cada arquivo em blocos e em segundo plano, o que permite arquivos de mais de 1 GB. Nada é enviado para a internet.
2. **Abrir o `resultado.json`** gerado pela ferramenta em Python, que traz também a conferência pelo IfcOpenShell e a exportação pelo IfcCSV.

Em qualquer dos dois casos as tabelas (resumo por grupo, elementos construídos, entidades, classificação, conjuntos de quantidades, modelos e entidades por modelo) têm um botão para baixar em CSV, com `;` como separador e vírgula decimal, prontas para o Excel. O resultado também pode ser baixado como `resultado.json`. A opção de usar códigos (ARQ-01, ...) no lugar dos nomes dos arquivos vem marcada.

O navegador faz só a contagem por texto. A conferência com o IfcOpenShell, a assinatura SHA-256 e a classificação por elemento (IfcCSV) ficam com a ferramenta em Python. Ao rodar a ferramenta em Python, ela grava também um `relatorio.html`, que é o visualizador com os resultados já dentro.

## Instalação

Python 3.10 ou mais novo.

```
python -m venv .venv
.venv\Scripts\activate        (Windows)   ou   source .venv/bin/activate
pip install -r requirements.txt
```

As versões ficam fixadas em `requirements.txt` (IfcOpenShell e IfcCSV 0.8.5) para que o resultado possa ser repetido.

## Uso

1. Copie `config_exemplo.json` para `config_local.json` e indique a pasta de cada disciplina, a pasta de saída e o caminho do mapa local. A ordem das disciplinas no arquivo é a ordem das colunas nas saídas.
2. Rode:

```
python inventario_ifc.py --config config_local.json
```

Para rodar só uma parte: `--etapas texto`, `--etapas ifcopenshell` ou `--etapas resumo` (separadas por vírgula). As etapas guardam o progresso em `_estado_*.json` na pasta de saída, e uma execução interrompida continua de onde parou.

Arquivos grandes (acima de 300 MB) são abertos um de cada vez no IfcOpenShell, porque um modelo de 1 GB pode ocupar vários gigabytes de memória.

## Anonimização

Os modelos aparecem nas saídas com um código (`ARQ-01`, `INT-01`, ...), dado pela ordem alfabética do nome do arquivo dentro de cada pasta. A ligação entre código e nome real vai para o arquivo `mapa_local`, que fica fora das saídas e não deve ser publicado (`_local/` e `config_local.json` estão no `.gitignore`). As exportações do IfcCSV trazem GlobalId, classe e classificação de cada elemento; confira se isso pode ser divulgado antes de publicar a pasta `ifccsv/`.

## Saídas

| Arquivo | Conteúdo |
|---|---|
| `manifesto.csv` | código, disciplina, esquema, tamanho e SHA-256 de cada arquivo de entrada |
| `entidades_por_modelo.csv` | contagem de cada entidade em cada modelo, pelos dois métodos |
| `conferencia_divergencias.csv` | diferenças entre os métodos (vazio quando tudo bate) |
| `entidades_por_disciplina.csv` | cada entidade, seu grupo e a contagem por disciplina |
| `grupos_por_disciplina.csv` | instâncias e percentual de cada grupo por disciplina |
| `elementos_construidos.csv` | classes de elemento construído por disciplina, total e parcela de `IfcBuildingElementProxy` |
| `classificacao_por_modelo.csv` | relações de classificação, elementos classificados (IfcCSV), sistemas declarados e nomes de Psets |
| `conjuntos_quantidades_por_modelo.csv` e `..._por_disciplina.csv` | nomes dos `IfcElementQuantity` e quantas vezes aparecem |
| `ifccsv/<modelo>.csv` | exportação do IfcCSV de cada modelo |
| `versoes.json` | versões do Python, do IfcOpenShell e do IfcCSV usadas |
| `resultado.json` | todos os resultados num arquivo só, para abrir no visualizador |
| `relatorio.html` | o visualizador com os resultados embutidos (abre sem servidor) |

O manifesto permite a quem tem acesso aos mesmos modelos confirmar, pelo SHA-256, que está usando exatamente os mesmos arquivos.
