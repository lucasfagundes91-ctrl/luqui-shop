# Marca da loja (ShopPro) — variáveis MARCA_*

O mesmo código roda um deploy por loja (serviço Railway + Postgres próprios).
Tudo que muda de uma loja pra outra é lido do ambiente em `marca.py` e chega
nos templates como `marca` (context processor + global do Jinja).

**Sem nenhuma variável MARCA_* setada, o site é o da Luqui Brinquedos,
idêntico ao de antes.** Por isso: variável apagada = valor da Luqui. Numa loja
nova, sete tudo o que aparece pro cliente (veja `casadosplasticos.env.example`).

- Vazio = usa o padrão. `-` = deixa o campo vazio (ex.: `MARCA_FACEBOOK=-`).
- Booleanos: `1`/`0`.
- JSON vai cru na linha (Raw Editor do Railway).

## Identidade

| Variável | Padrão (Luqui) | Onde aparece |
|---|---|---|
| `MARCA_NOME` | Luqui Brinquedos | títulos, rodapé, JSON-LD, e-mails, WhatsApp, cobrança Asaas |
| `MARCA_NOME_CURTO` | Luqui | "Sobre a …", "Siga a …", "Entrega … (local)", título do app no iPhone |
| `MARCA_RAZAO_SOCIAL` | Luqui Brinquedos LTDA | rodapé, privacidade |
| `MARCA_CNPJ` | 32.650.888/0001-02 | rodapé, privacidade |
| `MARCA_SLOGAN` | Aqui você compra brincando | título padrão, rodapé, /sobre |
| `MARCA_FRASE_RODAPE` | A loja de brinquedos da família. | rodapé, hero sem banner |
| `MARCA_SEGMENTO` | brinquedos | `brinquedos` mantém a copy de vitrine da Luqui (home, /sobre, 404, termos); outro valor usa texto genérico |
| `MARCA_TIPO_LOJA` | loja de brinquedos | título/H1 da home, /sobre, prompt da IA |
| `MARCA_ITEM` / `MARCA_ITENS` | brinquedo / brinquedos | "O … já está no seu carrinho", vitrines, avise-me, trocas |
| `MARCA_META_DESCRICAO` | (descrição atual) | `<meta description>` padrão |
| `MARCA_OG_DESCRICAO` | (descrição atual) | `og:description` padrão |
| `MARCA_SCHEMA_TIPO` | ToyStore | `@type` do JSON-LD |
| `MARCA_EMOJI` | 🧸 | fecho de WhatsApp/e-mail, 404, /sobre |
| `MARCA_DESCRITOR_FATURA` | LUQUIBRINQ | nome na fatura do cartão (Pagar.me, até 13 letras) |
| `MARCA_DESCONTO_ONLINE_PCT` | 15 | faixa "online você paga X% menos"; `0` some |

## Contato

| Variável | Padrão | Obs. |
|---|---|---|
| `MARCA_TELEFONE_EXIBICAO` | (45) 99111-9800 | texto |
| `MARCA_TELEFONE_E164` | +5545991119800 | JSON-LD; padrão de `WHATSAPP_LOJA`/`ADMIN_WHATSAPP` quando elas não estão setadas |
| `MARCA_EMAIL` | contato@luquibrinquedos.com.br | rodapé, JSON-LD; padrão de `EMAIL_FROM`/`EMAIL_REPLY_TO` |
| `MARCA_EMAIL_PRIVACIDADE` | privacidade@… (Luqui) / `MARCA_EMAIL` (outra loja) | página de privacidade |
| `MARCA_INSTAGRAM` | luquibrinquedos | só o @, sem URL; `-` esconde |
| `MARCA_FACEBOOK` | luquibrinquedos | idem |

## Endereços e horário

`MARCA_ENDERECOS` — lista JSON; **o 1º é a loja principal** (JSON-LD, CEP
padrão de frete/antifraude, cidade do frete fixo, foro dos termos). Rodapé,
/sobre, home, Retire na loja, entregas, WhatsApp de pedido pronto e o prompt da
IA listam todos (com uma loja só, o texto sai igual ao antigo).

```json
[{"nome":"Luqui Brinquedos","logradouro":"R. Engenheiro Rebouças","numero":"2053",
  "bairro":"Centro","cidade":"Cascavel","uf":"PR","cep":"85812-130",
  "obs":"Estacionamento gratuito em frente","curto":"R. Eng. Rebouças, 2053"}]
```

`curto` (opcional) é a forma abreviada usada em WhatsApp; sem ele vale
"logradouro, número". `obs` sai embaixo do endereço (estacionamento etc.).

| Variável | Padrão | Obs. |
|---|---|---|
| `MARCA_CEP_ORIGEM` | 85801080 | origem do Melhor Envio (config padrão) e CEP da loja quando o 1º endereço não tem CEP |
| `MARCA_HORARIO` | ["Seg a sex: 09:00 às 18:00", …] | linhas do Retire na loja e do prompt da IA |
| `MARCA_HORARIO_SCHEMA` | seg-sex 9-18, sáb 9-13 | JSON-LD: `[{"dias":[…],"abre":"09:00","fecha":"18:00"}]` |
| `MARCA_HORARIO_SOBRE`, `MARCA_HORARIO_RESUMO`, `MARCA_HORARIO_WHATSAPP`, `MARCA_HORARIO_TEXTO` | textos da Luqui | variações que o site da Luqui já tinha (/sobre, frete, WhatsApp, entregas). Se `MARCA_HORARIO` estiver setado e estas não, todas derivam dele |

## Visual

| Variável | Padrão |
|---|---|
| `MARCA_LOGO` | /static/logo.png |
| `MARCA_FAVICON` | /static/favicon.png |
| `MARCA_APPLE_TOUCH_ICON` | /static/apple-touch-icon.png |
| `MARCA_ICONE` / `MARCA_ICONE_ADMIN` | luqui-icon / luqui-admin-icon (nome-base em `static/brand/`, precisa existir -180, -192 e -512) |
| `MARCA_SLUG` | luqui (identificador do perfil de atalhos do iPhone) |
| `MARCA_COR_TEMA` | #f5b20a (theme-color, PWA) |
| `MARCA_COR_PRIMARIA` / `_ESCURA` / `_CLARA` | #1652C7 / #0E3D9E / #3B82F6 (`--azul`, `--azul-d`, `--azul-c`) |
| `MARCA_COR_DESTAQUE` / `_ESCURA` | #FFC700 / #E0A800 (`--amarelo`, `--amarelo-d`) |

Cada deploy aponta `MARCA_LOGO` pro próprio arquivo. A logo e os ícones da
Casa dos Plásticos estão em `static/marcas/` e `static/brand/casa-dos-plasticos-icon-*`
(gerados do PNG oficial); os da Luqui continuam onde sempre estiveram.

## Recursos

| Variável | Padrão | Obs. |
|---|---|---|
| `MARCA_ASSISTENTE_ATIVO` | 1 | chatbot do site (widget + `/api/luquizinha/*`); o roteiro da IA é de loja de brinquedos |
| `MARCA_ASSISTENTE_NOME` | Luquizinha | |
| `MARCA_LISTA_ANIVERSARIO_ATIVA` | 1 | `0` some com botões, menu e rotas (`/lista-aniversario`, `/minhas-listas`, `/lista/…`, `/api/lista(s)/…` → 404) |
| `MARCA_CLUBE_NOME` | Clube Luqui | o liga/desliga continua sendo `CLUBE_LUQUI_ATIVO` |
| `MARCA_ADMIN_NOME` / `MARCA_ADMIN_TITULO` | LuquiShop / LuquiShop Admin (outra loja: o nome dela) | títulos do painel |
| `MARCA_GESTAO_NOME` | `<nome curto>` Gestão | app do painel no celular |
| `MARCA_SISTEMA_AJUSTE` | Luqui Shop (outra loja: o nome) | `data-sistema` do botão "Pedir ajuste" |

## Variáveis antigas que também mudam por loja

`SITE_URL` (links, og:url, sitemap, JSON-LD), `WHATSAPP_LOJA`,
`ADMIN_WHATSAPP`, `EMAIL_FROM`, `EMAIL_REPLY_TO`, `PDVPRO_API_KEY`,
`CLUBE_LUQUI_ATIVO`, chaves de pagamento (Asaas/Pagar.me) e de Melhor Envio.

## O que NÃO muda por loja

- Identificadores internos: funções JS (`luquiConfirm`…), ids/classes CSS
  (`luqz-*`, `luqui-*`), chaves de localStorage, rotas (`/api/luquizinha/…`,
  `/liquida-luqui`), tabelas/colunas, cookies.
- A lista pré-calculada de municípios a 300 km (antifraude do cartão) é de
  Cascavel — serve pras duas lojas de hoje; loja em outra cidade precisa
  recalcular.
- Planos/benefícios do clube (seed) e o roteiro da IA são de loja de brinquedos.
