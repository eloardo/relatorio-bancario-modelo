# Extrato Bancos — relatório bancário diário

Todo dia às **06:47** chega no seu e-mail: saldo de cada conta, movimentações de ontem, entradas e saídas dos últimos 7 dias, maiores gastos, pagamentos agendados e o extrato dos últimos 3 dias.

- **Seus dados ficam só com você:** você usa a sua conta do Meu Pluggy, do GitHub e do Gmail. Ninguém mais recebe seus dados ou senhas.
- **Somente leitura:** o script não faz pagamentos nem altera nada nas contas.
- **Custo:** zero (Meu Pluggy pessoal + GitHub Actions + Gmail).

## Instalação

Siga o **guia do testador** que você recebeu junto com o convite. Resumo:

1. Clique em **Use this template → Create a new repository**, dê um nome e marque **Private** (obrigatório).
2. Em **Settings → Secrets and variables → Actions**, crie os 6 secrets:

| Nome do secret | O que colar |
|---|---|
| `PLUGGY_CLIENT_ID` | Client ID da sua aplicação no dashboard.pluggy.ai |
| `PLUGGY_CLIENT_SECRET` | Client Secret (aparece uma vez só) |
| `PLUGGY_ITEM_IDS` | Item IDs dos seus bancos, separados por vírgula, sem espaço |
| `EMAIL_REMETENTE` | o Gmail que envia |
| `EMAIL_SENHA_APP` | a senha de app de 16 letras desse Gmail |
| `EMAIL_DESTINO` | onde você quer receber |

3. Aba **Actions** → "Relatório bancário diário" → **Run workflow**. Em ~1 minuto o e-mail chega.

> Nunca mande esses valores para ninguém, nem para quem te convidou. Se der erro, mande só um print da tela de erro.

## Ajustes comuns

- **Horário:** linha `cron` em `.github/workflows/relatorio-diario.yml`, em UTC (Brasília + 3h). Ex.: 08:00 = `"0 11 * * *"`.
- **Só dias úteis:** troque o último `*` por `1-5`.
- **Mais de um destinatário:** `EMAIL_DESTINO` aceita vários, separados por vírgula.

## Como ler o relatório

- **Saldo total** soma conta corrente e poupança. Cartões aparecem à parte e não entram nas entradas/saídas, para não contar duas vezes o pagamento da fatura.
- **Transferências entre suas contas** aparecem como saída em uma e entrada na outra.
- Lançamentos do Open Finance podem levar algumas horas para aparecer; o rodapé mostra quando cada banco foi sincronizado.
- O log da aba Actions não mostra valores, só quantas contas foram lidas e se houve erro.
