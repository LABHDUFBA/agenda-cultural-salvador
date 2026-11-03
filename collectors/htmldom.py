"""DOM mínimo em biblioteca padrão.

O runtime do projeto não tem dependências externas (``requirements.txt`` é
vazio de propósito), então os coletores não podem usar BeautifulSoup/lxml.
Este módulo implementa o subconjunto que a coleta precisa:

* construir árvore tolerante a HTML malformado (:class:`Documento`);
* buscar por tag/classe em ordem de documento;
* extrair texto visível (sem ``script``/``style``).

Não é um parser HTML completo: não faz CSS, XPath nem normaliza entidades
além do que ``html.parser`` já resolve com ``convert_charrefs=True``.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Callable, Iterable, Iterator

# Conteúdo que nunca é texto visível e portanto não é percorrido.
IGNORAR = frozenset({"script", "style", "noscript", "template", "svg", "head"})
VAZIAS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)
BLOCO = frozenset(
    {
        "address", "article", "aside", "blockquote", "div", "dl", "dd", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "ol", "p",
        "pre", "section", "table", "tr", "td", "th", "ul",
    }
)


class No:
    """Nó de elemento ou de texto."""

    __slots__ = ("tag", "attrs", "children", "parent", "data")

    def __init__(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        parent: "No | None" = None,
        data: str | None = None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[No] = []
        self.parent = parent
        self.data = data

    # -- atributos ---------------------------------------------------------
    def get(self, nome: str, padrao: str = "") -> str:
        return self.attrs.get(nome, padrao)

    @property
    def classes(self) -> set[str]:
        return set(self.get("class").split())

    def tem_classe(self, *nomes: str) -> bool:
        return bool(self.classes.intersection(nomes))

    # -- navegação ---------------------------------------------------------
    def __iter__(self) -> Iterator["No"]:
        yield self
        for filho in self.children:
            yield from filho

    def elementos(self) -> Iterator["No"]:
        for no in self:
            if no.tag != "#text":
                yield no

    def find_all(
        self,
        tag: str | None = None,
        class_: str | None = None,
        id_: str | None = None,
        predicado: Callable[["No"], bool] | None = None,
    ) -> list["No"]:
        achados: list[No] = []
        for no in self.elementos():
            if tag is not None and no.tag != tag:
                continue
            if class_ is not None and class_ not in no.classes:
                continue
            if id_ is not None and no.get("id") != id_:
                continue
            if predicado is not None and not predicado(no):
                continue
            achados.append(no)
        return achados

    def find(self, tag: str | None = None, class_: str | None = None, **kw) -> "No | None":
        achados = self.find_all(tag, class_, **kw)
        return achados[0] if achados else None

    def texto(self, separador: str = " ") -> str:
        """Texto visível do nó, na ordem do documento."""
        partes: list[str] = []
        self._coletar_texto(partes)
        bruto = separador.join(p for p in partes if p)
        return " ".join(bruto.split())

    def _coletar_texto(self, partes: list[str]) -> None:
        for filho in self.children:
            if filho.tag == "#text":
                partes.append(filho.data or "")
            elif filho.tag in IGNORAR:
                continue
            else:
                filho._coletar_texto(partes)
                if filho.tag in BLOCO:
                    partes.append(" ")

    def __repr__(self) -> str:  # pragma: no cover - depuração
        return f"<No {self.tag} classes={sorted(self.classes)}>"


class _Construtor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raiz = No("#document")
        self.atual = self.raiz
        self._ignorar_profundidade = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in IGNORAR:
            self._ignorar_profundidade += 1
        dicionario = {k: (v or "") for k, v in attrs}
        no = No(tag, dicionario, parent=self.atual)
        self.atual.children.append(no)
        if tag not in VAZIAS:
            self.atual = no

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        dicionario = {k: (v or "") for k, v in attrs}
        self.atual.children.append(No(tag, dicionario, parent=self.atual))

    def handle_endtag(self, tag: str) -> None:
        if tag in IGNORAR and self._ignorar_profundidade:
            self._ignorar_profundidade -= 1
        no = self.atual
        while no is not None and no.tag != tag:
            no = no.parent
        if no is not None and no.parent is not None:
            self.atual = no.parent

    def handle_data(self, data: str) -> None:
        if self._ignorar_profundidade:
            return
        if data.strip():
            self.atual.children.append(No("#text", parent=self.atual, data=data))


def parse(html: str) -> No:
    """Converte HTML em árvore de nós (tolerante a markup quebrado)."""
    construtor = _Construtor()
    try:
        construtor.feed(html or "")
        construtor.close()
    except Exception:  # pragma: no cover - html.parser é tolerante; guarda extra
        pass
    return construtor.raiz


def em_ordem(raiz: No, tags: Iterable[str] | None = None) -> list[No]:
    """Elementos em ordem de documento, opcionalmente filtrados por tag."""
    permitidas = set(tags) if tags else None
    return [
        no
        for no in raiz.elementos()
        if permitidas is None or no.tag in permitidas
    ]


__all__ = ["BLOCO", "IGNORAR", "No", "em_ordem", "parse"]
