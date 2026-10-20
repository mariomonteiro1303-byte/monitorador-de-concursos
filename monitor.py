import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
from bs4 import BeautifulSoup

def monitorar_concursos():
    url = "https://exemplo-site-concursos.com.br"
    headers = {"User-Agent": "Mozilla/5.0"}
    editais = []
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            
            for item in soup.select(".card-concurso"):
                titulo = item.select_one(".titulo").get_text(strip=True) if item.select_one(".titulo") else "N/A"
                escolaridade = item.select_one(".escolaridade").get_text(strip=True) if item.select_one(".escolaridade") else "N/A"
                periodo_inscricao = item.select_one(".inscricoes").get_text(strip=True) if item.select_one(".inscricoes") else "N/A"
                local = item.select_one(".local").get_text(strip=True) if item.select_one(".local") else "N/A"
                
                editais.append({
                    "Órgão/Concurso": titulo,
                    "Escolaridade": escolaridade,
                    "Inscrições": periodo_inscricao,
                    "Local": local
                })
    except Exception as e:
        print(f"Erro ao raspar dados: {e}")
        
    return editais

def enviar_email(relatorio_texto):
    smtp_server = "smtp.gmail.com"
    port = 587
    
    # Pega as credenciais de forma segura direto das variáveis de ambiente do GitHub
    remetente = os.environ.get("EMAIL_REMETENTE")
    senha = os.environ.get("EMAIL_SENHA")
    destinatario = os.environ.get("EMAIL_DESTINATARIO")

    msg = MIMEMultipart()
    msg['From'] = remetente
    msg['To'] = destinatario
    msg['Subject'] = "Relatório Diário: Novos Editais de Concursos"

    msg.attach(MIMEText(relatorio_texto, 'plain', 'utf-8'))

    try:
        server = smtplib.SMTP(smtp_server, port)
        server.starttls()
        server.login(remetente, senha)
        server.sendmail(remetente, destinatario, msg.as_string())
        server.quit()
        print("E-mail enviado com sucesso!")
    except Exception as e:
        print(f"Erro ao enviar o e-mail: {e}")

if __name__ == "__main__":
    editais = monitorar_concursos()
    
    if editais:
        corpo_mensagem = "Foram encontrados os seguintes editais:\n\n"
        for edital in editais:
            corpo_mensagem += f"• Órgão: {edital['Órgão/Concurso']}\n" \
                              f"  Escolaridade: {edital['Escolaridade']}\n" \
                              f"  Inscrições: {edital['Inscrições']}\n" \
                              f"  Local: {edital['Local']}\n" + "-"*30 + "\n"
    else:
        corpo_mensagem = "Nenhum edital novo encontrado na última verificação de hoje."

    enviar_email(corpo_mensagem)