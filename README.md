# Monitor de Concursos

## O que esta versão faz

- Consulta páginas de fontes oficiais configuradas em `fontes.yml`.
- Prioriza links que parecem editais de abertura e exclui termos de retificação, resultado, convocação, nomeação e atos correlatos.
- Lê texto de páginas HTML e PDFs pesquisáveis (não faz OCR em PDFs escaneados).
- Produz os 11 campos padronizados, mantendo `Não informado` quando não há evidência suficiente.
- Acrescenta o status das inscrições quando as datas/texto permitem uma classificação cautelosa.
- Envia um e-mail com novos editais ou alterações de conteúdo.
- Usa `monitor_state.json` para evitar o reenvio diário de um edital sem alterações.
- Propaga falha de SMTP ou ausência total de fontes acessíveis para que o GitHub Actions mostre falha real.

## Fontes e cobertura

As fontes em `fontes.yml` são uma primeira cobertura confirmada de páginas oficiais do Piauí, ALECE/Ceará, Pernambuco, Bahia e uma fonte municipal do Maranhão. A cobertura não é exaustiva para todos os órgãos/municípios. O Tocantins ainda precisa de um portal central funcional confirmado para ser habilitado sem inventar endpoint. A extração de páginas específicas precisa ser verificada pela execução real do GitHub Actions.

## Segredos do GitHub

Em Settings > Secrets and variables > Actions, configure:

- `EMAIL_REMETENTE`: endereço Gmail que envia o relatório.
- `EMAIL_SENHA`: senha de app do Gmail.
- `EMAIL_DESTINATARIO`: endereço que recebe o relatório.

O código usa `smtp.gmail.com`, porta 587 e STARTTLS.

## Testes

Instale as dependências e execute:

```bash
python -m pip install -r requirements.txt
python -m py_compile monitor.py test_smtp.py
python -m unittest discover -s tests -v
```

Teste de e-mail manual no GitHub: Actions > Teste de envio de e-mail > Run workflow.

Teste completo: Actions > Monitor de Concursos > Run workflow.

## Persistência de estado

O workflow tem `contents: write` para gravar as chaves das oportunidades notificadas em `monitor_state.json`. Se as políticas do repositório bloquearem o token de Actions para fazer push, a etapa de persistência falhará; habilite permissões de leitura e gravação do `GITHUB_TOKEN` ou retire a persistência e aceite relatórios repetidos.
