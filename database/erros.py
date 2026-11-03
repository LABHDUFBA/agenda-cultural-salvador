"""Exceções do núcleo de dados."""


class ErroBanco(Exception):
    """Falha de configuração ou de integridade do banco."""


class DadoInvalido(ValueError):
    """Entrada recusada antes de qualquer escrita."""


class TransicaoInvalida(ErroBanco):
    """Mudança de status não permitida para o estado atual do evento."""
