"""Teste manual de envio SMTP Gmail; não imprime ou registra segredos."""
import os
import smtplib
import sys
from email.message import EmailMessage


def main() -> int:
    """Autentica no SMTP e envia uma mensagem simples ao destinatário configurado."""
    remetente = os.getenv("EMAIL_REMETENTE")
    senha = os.getenv("EMAIL_SENHA")
    destinatario = os.getenv("EMAIL_DESTINATARIO")
    faltantes = [nome for nome, valor in (
        ("EMAIL_REMETENTE", remetente), ("EMAIL_SENHA", senha),
        ("EMAIL_DESTINATARIO", destinatario),
    ) if not valor]
    if faltantes:
        print("FALHA: secrets/variáveis ausentes: " + ", ".join(faltantes))
        return 1
    mensagem = EmailMessage()
    mensagem["From"] = remetente
    mensagem["To"] = destinatario
    mensagem["Subject"] = "Teste SMTP - Monitor de Concursos"
    mensagem.set_content(
        "Este é um teste de envio do Monitor de Concursos via Gmail SMTP.\n"
        "Se esta mensagem chegou, o envio SMTP foi aceito. A coleta de concursos é validada separadamente."
    )
    try:
        with smtplib.SMTP(os.getenv("SMTP_SERVIDOR", "smtp.gmail.com"), int(os.getenv("SMTP_PORTA", "587")), timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(remetente, senha)
            smtp.send_message(mensagem)
        print(f"SUCESSO: SMTP aceitou o envio para {destinatario}. Confira a caixa de entrada ou Spam.")
        return 0
    except (smtplib.SMTPException, OSError, TimeoutError) as erro:
        print(f"FALHA SMTP ({type(erro).__name__}): {erro}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
