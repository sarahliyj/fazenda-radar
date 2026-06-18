# Fazenda Radar 🌾

**Radar de oportunidades em leilões de imóveis rurais.**

Raspa lotes distressed de megaleiloes.com.br, compara contra benchmarks regionais de preço por hectare e calcula um score de oportunidade 0–100. Resultados em painel web com exportação Excel.

---

## Requisitos

- **Python 3.9 ou superior** — [python.org/downloads](https://python.org/downloads)
- Conexão com a internet (para scraping e instalação de dependências)

---

## Início Rápido (Usuário Não-Técnico)

### macOS
1. Abra a pasta `fazenda_radar`
2. Dê **duplo clique** em `Iniciar Fazenda Radar.command`
3. Se aparecer aviso de segurança: clique com botão direito → Abrir → Abrir
4. O navegador abrirá automaticamente em `http://localhost:8501`
5. Clique em **"Buscar Leilões Agora"** no menu lateral

### Windows
1. Abra a pasta `fazenda_radar`
2. Dê **duplo clique** em `Iniciar Fazenda Radar.bat`
3. O navegador abrirá automaticamente em `http://localhost:8501`
4. Clique em **"Buscar Leilões Agora"** no menu lateral

> Na **primeira execução**, o instalador demora alguns minutos para baixar as dependências. As próximas execuções são instantâneas.

---

## Estrutura do Projeto

```
fazenda_radar/
├── Iniciar Fazenda Radar.command   # Launcher macOS (duplo clique)
├── Iniciar Fazenda Radar.bat       # Launcher Windows (duplo clique)
├── requirements.txt
├── scrapers/
│   ├── megaleiloes.py              # Scraper principal
│   └── __init__.py
├── data/
│   ├── benchmarks.py               # Preços de referência R$/ha por UF e tipo
│   ├── scorer.py                   # Calculador de score de oportunidade
│   └── __init__.py
├── dashboard/
│   ├── app.py                      # Aplicação Streamlit
│   └── __init__.py
├── exports/                        # Arquivos Excel exportados
└── assets/                         # Logos e recursos estáticos
```

---

## Score de Oportunidade (0–100)

| Componente | Peso | Critério |
|---|---|---|
| Desconto ao mercado | 60 pts | % abaixo do valor médio de mercado |
| Completude dos dados | 20 pts | Preço e hectares conhecidos |
| Urgência | 10 pts | Leilão nos próximos 30 dias |
| Confiança do benchmark | 10 pts | Estado com dados específicos |

**Notas:** A (80–100) · B (60–79) · C (40–59) · D (20–39) · F (0–19)

---

## Benchmarks de Preço (R$/ha)

Fontes: FNP Consultoria (Agrianual), INCRA, EMBRAPA, Scot Consultoria.
Valores de referência conservadores para análise de ativos distressed.
Atualize anualmente em `data/benchmarks.py`.

---

## Uso via Linha de Comando (Avançado)

```bash
# Instalar dependências manualmente
pip install -r requirements.txt

# Rodar só o scraper (3 páginas)
python -m scrapers.megaleiloes

# Abrir o painel
streamlit run dashboard/app.py
```

---

## Próximos Passos (Roadmap)

- [ ] Scrapers para e-leiloes.com.br e leilaoimovel.com.br
- [ ] Integração com API do INCRA para validação de área
- [ ] Alertas por e-mail para novos lotes com score A
- [ ] Cache local em SQLite para histórico de preços
- [ ] Modo comparação: mesma propriedade em múltiplos leilões
