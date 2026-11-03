"""HTTP honesto: identificação, robots.txt, limites e bloqueios respeitados.

Decisões deliberadas (e testadas em ``tests/test_collectors.py``):

* **sem disfarce.** O User-Agent identifica o projeto e um contato. Não há
  rotação de UA, proxy, nem navegador headless para vencer WAF.
* **sem retry em bloqueio.** 401/403/429 encerram a coleta com
  :class:`FonteBloqueada`; repetir com outra identidade seria contornar
  controle de acesso.
* **robots.txt é consultado** antes de cada host, com cache por processo.
  Se o robots não puder ser lido, seguimos a convenção RFC 9309 (permissivo)
  e registramos o fato na página baixada.
* **limites de tempo e tamanho** evitam que uma fonte derrube o coletor.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = (
    "AgendaCulturalSalvadorBot/1.0 "
    "(coletor de agenda cultural; +https://github.com/agenda-cultural-salvador)"
)
ACEITOS = ("text/html", "application/xhtml+xml", "application/json", "text/plain", "application/xml", "text/xml")
TAMANHO_MAXIMO = 5 * 1024 * 1024


class ErroColeta(Exception):
    """Falha de coleta já classificada (nunca silenciosa)."""

    def __init__(self, mensagem: str, *, fonte_id: str = "", url: str = "") -> None:
        super().__init__(mensagem)
        self.fonte_id = fonte_id
        self.url = url

    def to_dict(self) -> dict[str, str]:
        return {
            "erro": type(self).__name__,
            "mensagem": str(self),
            "fonte_id": self.fonte_id,
            "url": self.url,
        }


class FonteBloqueada(ErroColeta):
    """A fonte respondeu recusando o acesso (401/403/429). Não se insiste."""

    def __init__(self, mensagem: str, *, status: int, **kw) -> None:
        super().__init__(mensagem, **kw)
        self.status = status


class FonteIndisponivel(ErroColeta):
    """Rede, DNS, timeout ou erro 5xx: a fonte não está alcançável agora."""


class RobotsProibido(ErroColeta):
    """robots.txt veda a coleta nesta URL."""


class FonteNaoSuportada(ErroColeta):
    """A fonte exige execução de JS ou API privada: não é coletada."""


class RespostaInvalida(ErroColeta):
    """Status 4xx não relacionado a bloqueio, ou content-type inesperado."""


@dataclass(frozen=True, slots=True)
class PaginaBaixada:
    url_pedida: str
    url_final: str
    status: int
    content_type: str
    texto: str
    tamanho: int
    buscado_em: datetime
    duracao_s: float
    robots: str = "permitido"  # permitido | ausente | proibido
    cabecalhos: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "url_pedida": self.url_pedida,
            "url_final": self.url_final,
            "status": self.status,
            "content_type": self.content_type,
            "tamanho": self.tamanho,
            "buscado_em": self.buscado_em.isoformat(),
            "duracao_s": round(self.duracao_s, 3),
            "robots": self.robots,
        }


class HttpFetcher:
    """Cliente HTTP mínimo, sem dependências, com políticas explícitas."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        user_agent: str = USER_AGENT,
        respeitar_robots: bool = True,
        tamanho_maximo: int = TAMANHO_MAXIMO,
        intervalo_minimo: float = 1.0,
        dormir=time.sleep,
    ) -> None:
        if intervalo_minimo < 0:
            raise ValueError("intervalo_minimo não pode ser negativo")
        self.timeout = timeout
        self.user_agent = user_agent
        self.respeitar_robots = respeitar_robots
        self.tamanho_maximo = tamanho_maximo
        self.intervalo_minimo = intervalo_minimo
        self._dormir = dormir
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}
        self._ultimo_acesso: dict[str, float] = {}

    # -- infraestrutura ----------------------------------------------------
    def _cabecalhos(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = {
            "User-Agent": self.user_agent,
            "Accept": ", ".join(ACEITOS) + ";q=0.9,*/*;q=0.1",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if extra:
            base.update(extra)
        return base

    def _respeitar_intervalo(self, host: str) -> None:
        if self.intervalo_minimo <= 0:
            return
        anterior = self._ultimo_acesso.get(host)
        agora = time.monotonic()
        if anterior is not None:
            espera = self.intervalo_minimo - (agora - anterior)
            if espera > 0:
                self._dormir(espera)
        self._ultimo_acesso[host] = time.monotonic()

    def _robots_para(self, url: str) -> str:
        partes = urllib.parse.urlsplit(url)
        base = f"{partes.scheme}://{partes.netloc}"
        if base in self._robots:
            parser, estado = self._robots[base]
            if parser is None:
                return estado
        else:
            parser = None
            estado = "ausente"
            try:
                self._respeitar_intervalo(partes.netloc)
                pedido = urllib.request.Request(
                    base + "/robots.txt", headers=self._cabecalhos()
                )
                with urllib.request.urlopen(pedido, timeout=self.timeout) as resposta:
                    corpo = resposta.read(512 * 1024).decode("utf-8", errors="replace")
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(corpo.splitlines())
                estado = "permitido"
            except Exception:
                parser = None  # RFC 9309: ausência/erro => permitido
                estado = "ausente"
            self._robots[base] = (parser, estado)

        if parser is None:
            return "ausente"
        if not parser.can_fetch(self.user_agent, url):
            return "proibido"
        return "permitido"

    def _decodificar(self, bruto: bytes, content_type: str) -> str:
        charset = "utf-8"
        for parte in content_type.split(";")[1:]:
            chave, _, valor = parte.strip().partition("=")
            if chave.lower() == "charset" and valor:
                charset = valor.strip('"\' ')
        try:
            return bruto.decode(charset, errors="replace")
        except LookupError:
            return bruto.decode("utf-8", errors="replace")

    # -- API ---------------------------------------------------------------
    def baixar(self, url: str, *, fonte_id: str = "") -> PaginaBaixada:
        partes = urllib.parse.urlsplit(url)
        if partes.scheme not in ("http", "https") or not partes.netloc:
            raise RespostaInvalida(f"URL inválida: {url!r}", fonte_id=fonte_id, url=url)

        estado_robots = "permitido"
        if self.respeitar_robots:
            estado_robots = self._robots_para(url)
            if estado_robots == "proibido":
                raise RobotsProibido(
                    f"robots.txt veda a coleta em {url}",
                    fonte_id=fonte_id,
                    url=url,
                )

        pedido = urllib.request.Request(url, headers=self._cabecalhos())
        inicio = time.monotonic()
        self._respeitar_intervalo(partes.netloc)
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as resposta:
                status = int(resposta.status)
                content_type = resposta.headers.get("Content-Type", "")
                bruto = resposta.read(self.tamanho_maximo + 1)
                url_final = resposta.geturl()
                cabecalhos = dict(resposta.headers.items())
        except urllib.error.HTTPError as erro:
            status = int(erro.code)
            if status in (401, 403, 429):
                raise FonteBloqueada(
                    f"fonte recusou acesso: HTTP {status} em {url}",
                    status=status,
                    fonte_id=fonte_id,
                    url=url,
                ) from None
            if status >= 500:
                raise FonteIndisponivel(
                    f"fonte instável: HTTP {status} em {url}",
                    fonte_id=fonte_id,
                    url=url,
                ) from None
            raise RespostaInvalida(
                f"HTTP {status} em {url}", fonte_id=fonte_id, url=url
            ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as erro:
            motivo = getattr(erro, "reason", erro)
            raise FonteIndisponivel(
                f"não foi possível alcançar {url}: {motivo}",
                fonte_id=fonte_id,
                url=url,
            ) from None

        if len(bruto) > self.tamanho_maximo:
            bruto = bruto[: self.tamanho_maximo]
        tipo_base = content_type.split(";")[0].strip().lower()
        if tipo_base and tipo_base not in ACEITOS:
            raise RespostaInvalida(
                f"content-type não suportado ({tipo_base}) em {url}",
                fonte_id=fonte_id,
                url=url,
            )
        return PaginaBaixada(
            url_pedida=url,
            url_final=url_final,
            status=status,
            content_type=content_type,
            texto=self._decodificar(bruto, content_type),
            tamanho=len(bruto),
            buscado_em=datetime.now(timezone.utc),
            duracao_s=time.monotonic() - inicio,
            robots=estado_robots,
            cabecalhos=cabecalhos,
        )

    def sondar(self, url: str, *, fonte_id: str = "") -> dict[str, object]:
        """Teste de acesso que nunca levanta exceção: devolve o resultado."""
        try:
            pagina = self.baixar(url, fonte_id=fonte_id)
        except ErroColeta as erro:
            resultado = erro.to_dict()
            resultado["acessivel"] = False
            if isinstance(erro, FonteBloqueada):
                resultado["status"] = erro.status
            return resultado
        return {
            "acessivel": True,
            "status": pagina.status,
            "content_type": pagina.content_type,
            "tamanho": pagina.tamanho,
            "duracao_s": round(pagina.duracao_s, 3),
            "robots": pagina.robots,
        }


__all__ = [
    "ACEITOS",
    "ErroColeta",
    "FonteBloqueada",
    "FonteIndisponivel",
    "FonteNaoSuportada",
    "HttpFetcher",
    "PaginaBaixada",
    "RespostaInvalida",
    "RobotsProibido",
    "USER_AGENT",
]
