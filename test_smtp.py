"""Teste isolado de autenticação e envio SMTP do Gmail.

Usa somente variáveis de ambiente/Secrets. Nunca imprime a senha.
"""
import os
import smtplib
from email.message import EmailMessage


def main():
    """Valida credenciais e envia uma mensagem curta para o destinatário configurado."""
    remetente = os.getenv("EMAIL_REMETENTE")
    senha = os.getenv("EMAIL_SENHA")
    destinatario = os.getenv("EMAIL_DESTINATARIO")
    faltantes = [nome for nome, valor in {
        "EMAIL_REMETENTE": remetente,
        "EMAIL_SENHA": senha,
        "EMAIL_DESTINATARIO": destinatario,
    }.items() if not valor]
    if faltantes:
        raise SystemExit("Secrets ausentes: " + ", ".join(faltantes))

    mensagem = EmailMessage()
    mensagem["From"] = remetente
    mensagem["To"] = destinatario
    mensagem["Subject"] = "Teste SMTP - Monitor de Concursos"
    mensagem.set_content(
        "Este é um teste de envio SMTP do Monitor de Concursos.\n"
        "Se você recebeu esta mensagem, a autenticação e o envio funcionaram.\n"
        "Este teste não confirma a coleta de concursos."
    )

    servidor_smtp = os.getenv("SMTP_SERVIDOR", "smtp.gmail.com")
    porta_smtp = int(os.getenv("SMTP_PORTA", "587"))
    # Context manager fecha a conexão mesmo se ocorrer erro durante o envio.
    with smtplib.SMTP(servidor_smtp, porta_smtp, timeout=30) as servidor:
        servidor.ehlo()
        servidor.starttls()
        servidor.ehlo()
        servidor.login(remetente, senha)
        servidor.send_message(mensagem)
    print(f"Teste SMTP enviado com sucesso para {destinatario}.")


if __name__ == "__main__":
    main()
