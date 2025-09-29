import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import json
import datetime
import io  # Necessário para manipular o arquivo JSON como string

# --- 1. Configurações Base para diferentes tipos de células ---
ALL_CELL_SPECS = {
    "Sony": {
        "NOME": "Sony US18650VTC2",
        "FABRICANTE": "Sony",
        "FERRAMENTA_USO": "CLECO",
        "TENSAO_NOMINAL": 3.70,  # V (Célula Individual)
        "TENSAO_CARGA_MAX": 4.20,  # V (Célula Individual)
        "TENSAO_CORTE": 2.50,  # V (Célula Individual)
        "IR_NOVA_TIPICA": 16.00,  # mOhm (Resistência Interna Típica de Célula Individual Nova)
        "CORRENTE_MAX_CONTINUA": 30.00,  # A (Aprox. para VTC2)
        "CAPACIDADE_NOMINAL_MAH": 1550,  # mAh (sua referência)
    },
    "Panasonic": {
        "NOME": "Panasonic UR18650RX",
        "FABRICANTE": "Panasonic",
        "FERRAMENTA_USO": "Bosch",
        "TENSAO_NOMINAL": 3.60,  # V (Célula Individual - Seção 5.4 do PDF)
        "TENSAO_CARGA_MAX": 4.20,  # V (Célula Individual - Seção 5.7 do PDF)
        "TENSAO_CORTE": 2.75,  # V (Célula Individual - Seção 5.5 do PDF)
        "IR_NOVA_TIPICA": 20.00,
        # mOhm (Inferido de "less than 25mOhm" na Seção 5.10, um valor típico razoável abaixo do max)
        "CORRENTE_MAX_CONTINUA": 20.00,  # A (Seção 5.9 do PDF)
        "CAPACIDADE_NOMINAL_MAH": 1950,  # mAh (Seção 5.1 do PDF)
    }
}

# Limiares gerais para avaliação de Células Individuais (independentes do modelo, mas podem ser ajustados)
# OCV
OCV_DIFF_LIMIAR_MONITOR = 0.05  # V (50mV de diferença da média do pack)
OCV_DIFF_LIMIAR_RUIM = 0.10  # V (100mV de diferença da média do pack)
OCV_ABSOLUTO_LIMIAR_RUIM = 3.00  # V (tensão absoluta abaixo disso é preocupante, recarga urgente)
OCV_ABSOLUTO_LIMIAR_CRITICO = 2.50  # V (tensão absoluta abaixo disso indica célula potencialmente danificada)

# IR
IR_LIMIAR_BOM_MAX = 20.00  # mOhm (Até este valor ainda é considerado muito bom para célula usada)
IR_LIMIAR_MONITOR_MAX = 30.00  # mOhm (Entre 20-30mOhm requer monitoramento)
IR_LIMIAR_RUIM_MIN = 30.00  # mOhm (Acima de 30mOhm, a célula está degradada)

IR_PCT_DIFF_LIMIAR_MONITOR = 25  # % (Desvio percentual da IR da célula em relação à média do pack)
IR_PCT_DIFF_LIMIAR_RUIM = 50  # % (Desvio percentual da IR da célula em relação à média do pack)


# --- Funções para cálculo dinâmico de especificações do pack e avaliação ---
def calculate_pack_specs(cell_specs_individual, num_cells_series=12):  # num_cells_series default to 12
    pack_specs = {
        "NUM_CELULAS_SERIE": num_cells_series,
        "PACK_TENSAO_MAX": num_cells_series * cell_specs_individual["TENSAO_CARGA_MAX"],
        "PACK_TENSAO_NOMINAL": num_cells_series * cell_specs_individual["TENSAO_NOMINAL"],
        "PACK_TENSAO_CORTE": num_cells_series * cell_specs_individual["TENSAO_CORTE"],
    }
    # Limiares de Avaliação para a Tensão Total do Pack (medida diretamente no pack)
    pack_specs["PACK_TENSAO_LIMIAR_BOM_MIN"] = pack_specs["PACK_TENSAO_NOMINAL"] - (
                0.5 * (num_cells_series / 12))  # Scaling factor for generic thresholds
    pack_specs["PACK_TENSAO_LIMIAR_MONITOR_MIN"] = pack_specs["PACK_TENSAO_CORTE"] + (6.0 * (num_cells_series / 12))
    pack_specs["PACK_TENSAO_LIMIAR_RUIM_MIN"] = pack_specs["PACK_TENSAO_CORTE"] + (1.2 * (num_cells_series / 12))
    pack_specs["PACK_TENSAO_LIMIAR_CRITICO_MIN"] = pack_specs["PACK_TENSAO_CORTE"]

    return pack_specs


def avaliar_celula_individual(ocv, ir, current_cell_specs, ocv_media=None, ir_media=None, is_avulsa=False):
    status = "Bom"
    motivos = []

    # Avaliação de Tensão (OCV)
    if ocv < current_cell_specs["TENSAO_CORTE"]:
        status = "Crítico"
        motivos.append(
            f"OCV muito baixa (< {current_cell_specs['TENSAO_CORTE']:.2f}V). Risco de dano irreversível e segurança.")
    elif ocv < OCV_ABSOLUTO_LIMIAR_RUIM:
        if status != "Crítico":
            status = "Ruim"
            motivos.append(f"OCV baixa (< {OCV_ABSOLUTO_LIMIAR_RUIM:.2f}V). Requer recarga urgente.")

    if not is_avulsa and ocv_media is not None:  # Apenas para packs, onde a comparação entre células é relevante
        ocv_diff = abs(ocv - ocv_media)
        if ocv_diff > OCV_DIFF_LIMIAR_RUIM:
            if status not in ["Crítico", "Ruim"]:
                status = "Ruim"
                motivos.append(
                    f"OCV com alto desvio da média do pack ({ocv_diff:.2f}V). Indica forte desbalanceamento.")
        elif ocv_diff > OCV_DIFF_LIMIAR_MONITOR:
            if status == "Bom":
                status = "Monitorar"
                motivos.append(
                    f"OCV com desvio moderado da média do pack ({ocv_diff:.2f}V). Indica desbalanceamento inicial.")

    # Avaliação de Resistência Interna (IR)
    if ir >= IR_LIMIAR_RUIM_MIN:
        if status not in ["Crítico", "Ruim"]:
            status = "Ruim"
            motivos.append(f"IR muito alta ({ir:.2f} mOhm). Célula degradada, compromete potência e aquecimento.")
    elif ir >= IR_LIMIAR_MONITOR_MAX:
        if status == "Bom":
            status = "Monitorar"
            motivos.append(f"IR moderada ({ir:.2f} mOhm). Degradando, requer monitoramento. ")
    elif ir > IR_LIMIAR_BOM_MAX:
        if status == "Bom":
            status = "Monitorar"
            motivos.append(f"IR um pouco acima do ideal ({ir:.2f} mOhm). Pode ser sinal de envelhecimento.")

    if not is_avulsa and ir_media is not None and ir_media > 0:  # Apenas para packs
        ir_pct_diff = ((ir - ir_media) / ir_media) * 100
        if ir_pct_diff > IR_PCT_DIFF_LIMIAR_RUIM:
            if status not in ["Crítico", "Ruim"]:
                status = "Ruim"
                motivos.append(
                    f"IR com alto desvio percentual da média do pack ({ir_pct_diff:.1f}%). Sinal de degradação acentuada.")
        elif ir_pct_diff > IR_PCT_DIFF_LIMIAR_MONITOR:
            if status == "Bom":
                status = "Monitorar"
                motivos.append(
                    f"IR com desvio percentual moderado da média do pack ({ir_pct_diff:.1f}%). Início de degradação.")

    if not motivos and status == "Bom":
        motivos.append("Dentro dos parâmetros esperados.")

    return status, "; ".join(motivos) if motivos else "N/A"


def avaliar_pack_voltage(total_pack_voltage_medido, current_pack_specs):
    status_pack_v = "Bom"
    motivos_pack_v = []

    if total_pack_voltage_medido > current_pack_specs["PACK_TENSAO_MAX"] + 0.1:  # Pequena margem para erro de medição
        status_pack_v = "Crítico"
        motivos_pack_v.append(
            f"Tensão total do pack ({total_pack_voltage_medido:.2f}V) acima do máximo permitido ({current_pack_specs['PACK_TENSAO_MAX']:.2f}V). Risco de sobrecarga e superaquecimento.")
    elif total_pack_voltage_medido < current_pack_specs["PACK_TENSAO_LIMIAR_CRITICO_MIN"]:
        status_pack_v = "Crítico"
        motivos_pack_v.append(
            f"Tensão total do pack ({total_pack_voltage_medido:.2f}V) abaixo da tensão de corte ({current_pack_specs['PACK_TENSAO_LIMIAR_CRITICO_MIN']:.2f}V). Risco de dano irreversível às células e segurança comprometida.")
    elif total_pack_voltage_medido < current_pack_specs["PACK_TENSAO_LIMIAR_RUIM_MIN"]:
        status_pack_v = "Ruim"
        motivos_pack_v.append(
            f"Tensão total do pack ({total_pack_voltage_medido:.2f}V) muito baixa. Próximo à descarga crítica, a ferramenta pode falhar sob carga.")
    elif total_pack_voltage_medido < current_pack_specs["PACK_TENSAO_LIMIAR_MONITOR_MIN"]:
        status_pack_v = "Monitorar"
        motivos_pack_v.append(
            f"Tensão total do pack ({total_pack_voltage_medido:.2f}V) baixa. Sugere que o pack precisa ser recarregado em breve.")
    elif total_pack_voltage_medido < current_pack_specs["PACK_TENSAO_LIMIAR_BOM_MIN"]:
        status_pack_v = "Monitorar"
        motivos_pack_v.append(
            f"Tensão total do pack ({total_pack_voltage_medido:.2f}V) abaixo da nominal, mas aceitável. Indicativo de SoC intermediário.")
    else:
        motivos_pack_v.append(
            f"Tensão total do pack ({total_pack_voltage_medido:.2f}V) dentro dos limites esperados e saudáveis.")

    return status_pack_v, "; ".join(motivos_pack_v)


# Helper para estilização de status
def color_status(val):
    if val == "Crítico":
        return 'background-color: #ffcccc'  # Light red
    elif val == "Ruim":
        return 'background-color: #ffe6cc'  # Light orange
    elif val == "Monitorar":
        return 'background-color: #ffffcc'  # Light yellow
    elif val == "Bom":
        return 'background-color: #ccffcc'  # Light green
    return ''


# HTML Report Generation for Pack Analysis
def generate_html_report_pack(header_info, current_cell_specs, pack_voltage_info, df_results, fig_ocv_json, fig_ir_json,
                              status_pack_geral, status_pack_alert_html):
    # Recalculate pack specs within the function using the passed current_cell_specs
    # assuming `num_cells_series` is fixed at 12 for packs.
    num_cells_series_pack = 12  # Hardcoded as per problem description for packs
    current_pack_specs_for_report = calculate_pack_specs(current_cell_specs, num_cells_series=num_cells_series_pack)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Relatório de Análise de Bateria - Pack {header_info['numero_bateria']}</title>
        <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; color: #333; }}
            h1, h2, h3 {{ color: #2c3e50; }}
            .container {{ max-width: 900px; margin: auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            .header-info p {{ margin: 5px 0; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            .status-Bom {{ background-color: #ccffcc; }}
            .status-Monitorar {{ background-color: #ffffcc; }}
            .status-Ruim {{ background-color: #ffe6cc; }}
            .status-Crítico {{ background-color: #ffcccc; font-weight: bold; }}
            .alert-critical {{ background-color: #ffdddd; padding: 15px; border-radius: 5px; border: 1px solid red; margin-top: 20px; }}
            .alert-warning {{ background-color: #fff3cd; padding: 10px; border-radius: 5px; border: 1px solid #ffeeba; margin-top: 20px; }}
            .chart-container {{ margin-top: 30px; border: 1px solid #eee; padding: 15px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Relatório de Análise de Pack de Baterias</h1>
            <div class="header-info">
                <p><strong>Data do Teste:</strong> {header_info['data_teste']}</p>
                <p><strong>Nome do Técnico:</strong> {header_info['nome_tecnico']}</p>
                <p><strong>Número da Bateria (Pack):</strong> {header_info['numero_bateria']}</p>
                <p><strong>Identificação dos Terminais:</strong> {header_info['identificacao_terminais']}</p>
                <p><strong>Células Analisadas no Pack:</strong> {current_pack_specs_for_report['NUM_CELULAS_SERIE']}</p>
                <p><strong>Modelo da Célula:</strong> {current_cell_specs['NOME']} ({current_cell_specs['FABRICANTE']})</p>
                <p><strong>Ferramenta de Uso:</strong> {current_cell_specs['FERRAMENTA_USO']}</p>
            </div>

            <h2>Análise da Tensão Total do Pack</h2>
            <p><strong>Tensão Total Medida:</strong> {pack_voltage_info['total_pack_voltage_medido']:.2f} V</p>
            <p><strong>Status da Tensão Total:</strong> {pack_voltage_info['status_pack_v']} - {pack_voltage_info['motivos_pack_v']}</p>
            <p><strong>Soma das OCVs Individuais:</strong> {pack_voltage_info['ocv_soma_calculada']:.2f} V</p>
            {f"<div class='alert-warning'><p><strong>Aviso de Discrepância:</strong> {pack_voltage_info['discrepancy_warning']}</p></div>" if pack_voltage_info['discrepancy_warning'] else ""}

            <h2>Status Geral do Pack</h2>
            {status_pack_alert_html}

            <h2>Status Individual Detalhado das Células do Pack</h2>
            <table>
                <thead>
                    <tr>
                        <th>Célula</th>
                        <th>OCV (V)</th>
                        <th>Desvio OCV (V)</th>
                        <th>IR (mOhm)</th>
                        <th>Desvio IR (mOhm)</th>
                        <th>Status</th>
                        <th>Observações</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join([
        f"<tr class='status-{row['Status']}'>"
        f"<td>{row['Célula']}</td>"
        f"<td>{row['OCV (V)']}</td>"
        f"<td>{row['Desvio OCV (V)']}</td>"
        f"<td>{row['IR (mOhm)']}</td>"
        f"<td>{row['Desvio IR (mOhm)']}</td>"
        f"<td>{row['Status']}</td>"
        f"<td>{row['Observações']}</td>"
        f"</tr>"
        for index, row in df_results.iterrows()
    ])}
                </tbody>
            </table>

            <h2>Gráficos de Análise</h2>
            <div class="chart-container">
                <h3>OCV por Célula</h3>
                <div id="ocvChart"></div>
                <script>
                    var ocvData = {fig_ocv_json};
                    Plotly.newPlot('ocvChart', ocvData.data, ocvData.layout);
                </script>
            </div>

            <div class="chart-container">
                <h3>Resistência Interna (IR) por Célula</h3>
                <div id="irChart"></div>
                <script>
                    var irData = {fig_ir_json};
                    Plotly.newPlot('irChart', irData.data, irData.layout);
                </script>
            </div>

            <p style="margin-top: 40px; font-size: 0.9em; color: #777;">
                Relatório gerado automaticamente em {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}.
            </p>
        </div>
    </body>
    </html>
    """
    return html_content


# HTML Report Generation for Individual Cell Analysis
def generate_html_report_avulsas(header_info_avulsas, current_cell_specs, df_results_avulsas, fig_ocv_json_avulsas,
                                 fig_ir_json_avulsas):
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Relatório de Análise de Células Avulsas - {header_info_avulsas['data_geracao_relatorio']}</title>
        <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; color: #333; }}
            h1, h2, h3 {{ color: #2c3e50; }}
            .container {{ max-width: 900px; margin: auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            .header-info p {{ margin: 5px 0; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            .status-Bom {{ background-color: #ccffcc; }}
            .status-Monitorar {{ background-color: #ffffcc; }}
            .status-Ruim {{ background-color: #ffe6cc; }}
            .status-Crítico {{ background-color: #ffcccc; font-weight: bold; }}
            .chart-container {{ margin-top: 30px; border: 1px solid #eee; padding: 15px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Relatório de Análise de Células Avulsas</h1>
            <div class="header-info">
                <p><strong>Data de Geração do Relatório:</strong> {header_info_avulsas['data_geracao_relatorio']}</p>
                <p><strong>Nome do Técnico:</strong> {header_info_avulsas['nome_tecnico']}</p>
                <p><strong>Modelo da Célula:</strong> {current_cell_specs['NOME']} ({current_cell_specs['FABRICANTE']})</p>
                <p><strong>Ferramenta de Uso:</strong> {current_cell_specs['FERRAMENTA_USO']}</p>
                <p><strong>Quantidade de Células Testadas:</strong> {len(df_results_avulsas)}</p>
            </div>

            <h2>Resultados das Células Avulsas (Ordenadas da Melhor para a Pior)</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID da Célula</th>
                        <th>Data Teste</th>
                        <th>OCV (V)</th>
                        <th>IR (mOhm)</th>
                        <th>Status</th>
                        <th>Observações</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join([
        f"<tr class='status-{row['Status']}'>"
        f"<td>{row['ID da Célula']}</td>"
        f"<td>{row['Data Teste']}</td>"
        f"<td>{row['OCV (V)']}</td>"
        f"<td>{row['IR (mOhm)']}</td>"
        f"<td>{row['Status']}</td>"
        f"<td>{row['Observações']}</td>"
        f"</tr>"
        for index, row in df_results_avulsas.iterrows()
    ])}
                </tbody>
            </table>

            <h2>Gráficos de Análise</h2>
            <div class="chart-container">
                <h3>OCV por Célula (Ordenado)</h3>
                <div id="ocvChartAvulsas"></div>
                <script>
                    var ocvDataAvulsas = {fig_ocv_json_avulsas};
                    Plotly.newPlot('ocvChartAvulsas', ocvDataAvulsas.data, ocvDataAvulsas.layout);
                </script>
            </div>

            <div class="chart-container">
                <h3>Resistência Interna (IR) por Célula (Ordenado)</h3>
                <div id="irChartAvulsas"></div>
                <script>
                    var irDataAvulsas = {fig_ir_json_avulsas};
                    Plotly.newPlot('irChartAvulsas', irDataAvulsas.data, irDataAvulsas.layout);
                </script>
            </div>

            <p style="margin-top: 40px; font-size: 0.9em; color: #777;">
                Relatório gerado automaticamente em {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}.
            </p>
        </div>
    </body>
    </html>
    """
    return html_content


# --- Streamlit App Setup ---
st.set_page_config(layout="wide", page_title="Análise de Saúde de Baterias 18650")

st.sidebar.header("Sobre este Aplicativo")
st.sidebar.info(
        "Permite diagnosticar a saúde de packs de baterias de íon-lítio 18650, otimizando a manutenção preditiva e garantindo a segurança operacional."
)
st.sidebar.markdown(
    """
    **Lembre-se Sempre:**
    A segurança é a prioridade máxima ao trabalhar com baterias.
    *   Siga rigorosamente os procedimentos de segurança.
    *   Use Equipamentos de Proteção Individual (EPIs).
    *   Nunca tente reparar células danificadas ou sobrecarregadas.
    *   Descarte baterias de íon-lítio em locais apropriados para reciclagem.
    """
)

# --- Main Tabs ---
tab1, tab2 = st.tabs(["Análise de Pack de Baterias (12S)", "Análise de Células Avulsas"])

with tab1:
    st.title("🔋 Análise Abrangente de Saúde de Packs de Baterias")

    # Cell Type Selection for Pack Tab
    if 'selected_cell_type_pack' not in st.session_state:
        st.session_state.selected_cell_type_pack = "Sony"

    st.session_state.selected_cell_type_pack = st.selectbox(
        "Selecione o tipo de célula para o Pack:",
        list(ALL_CELL_SPECS.keys()),
        index=list(ALL_CELL_SPECS.keys()).index(st.session_state.selected_cell_type_pack),
        key="cell_type_selector_pack"
    )
    current_cell_specs_pack = ALL_CELL_SPECS[st.session_state.selected_cell_type_pack]
    current_pack_specs_pack = calculate_pack_specs(current_cell_specs_pack)  # Calculate pack specs dynamically

    st.markdown(
        f"""
        Esta ferramenta fornece uma avaliação detalhada da saúde das células individuais de um pack de
        baterias **{current_cell_specs_pack['NOME']} ({current_cell_specs_pack['FABRICANTE']}) de {current_pack_specs_pack['NUM_CELULAS_SERIE']}S**,
        utilizado em ferramentas **{current_cell_specs_pack['FERRAMENTA_USO']}**.
        Inclui a validação da **Tensão Total do Pack** para uma visão completa.

        **Especificações da Célula ({current_cell_specs_pack['NOME']}):**
        - Capacidade Nominal: {current_cell_specs_pack['CAPACIDADE_NOMINAL_MAH']}mAh
        - Tensão Nominal (Individual): {current_cell_specs_pack['TENSAO_NOMINAL']:.2f}V
        - Tensão Máxima de Carga (Individual): {current_cell_specs_pack['TENSAO_CARGA_MAX']:.2f}V
        - Tensão de Corte de Descarga (Individual): {current_cell_specs_pack['TENSAO_CORTE']:.2f}V
        - Resistência Interna Típica (Nova): {current_cell_specs_pack['IR_NOVA_TIPICA']:.2f} mOhm
        - Corrente Máx. Contínua: {current_cell_specs_pack['CORRENTE_MAX_CONTINUA']:.2f}A

        **Especificações do PACK ({current_pack_specs_pack['NUM_CELULAS_SERIE']}S):**
        - Tensão Nominal (Total): {current_pack_specs_pack['PACK_TENSAO_NOMINAL']:.2f}V
        - Tensão Máxima de Carga (Total): {current_pack_specs_pack['PACK_TENSAO_MAX']:.2f}V
        - Tensão Mínima de Corte (Total): {current_pack_specs_pack['PACK_TENSAO_CORTE']:.2f}V

        **Instruções para uma Análise Precisa:**
        1.  **Segurança:** Certifique-se de que a bateria esteja **desconectada** da ferramenta e carregador. Use **EPIs** apropriados.
        2.  Use o **Fnirsi HRM-10** para medir a **Resistência Interna (IR)** de cada célula individualmente (em mOhm).
        3.  Use um **multímetro digital** para medir a **Tensão de Circuito Aberto (OCV)** de cada célula individualmente (em Volts).
        4.  Com o multímetro, meça a **Tensão Total do Pack** diretamente nos terminais principais de saída da bateria (em Volts).
        5.  **Condição de Medição Ideal:** Realize as medições quando a bateria estiver **totalmente carregada** (próximo de {current_pack_specs_pack['PACK_TENSAO_MAX']:.2f}V para o pack, e {current_cell_specs_pack['TENSAO_CARGA_MAX']:.2f}V por célula) e **após 30 minutos de repouso** do carregamento para que as tensões se estabilizem.
        6.  Preencha o cabeçalho do teste e insira os valores coletados.
        """
    )

    st.divider()

    # --- Header Data Entry for Pack Tab ---
    st.header("Dados do Teste do Pack")

    if 'pack_header_data' not in st.session_state:
        st.session_state.pack_header_data = {
            "data_teste": datetime.date.today(),
            "nome_tecnico": "Diógenes Oliveira",
            "numero_bateria": 0,
            "identificacao_terminais": "B0 a B12 (Célula 1: B0-B1, Célula 2: B1-B2, ..., Célula 12: B11-B12)"
        }

    col_date, col_name = st.columns(2)
    with col_date:
        st.session_state.pack_header_data["data_teste"] = st.date_input("Data do Teste",
                                                                        value=st.session_state.pack_header_data[
                                                                            "data_teste"], key="pack_data_teste")
    with col_name:
        st.session_state.pack_header_data["nome_tecnico"] = st.text_input("Nome do Técnico",
                                                                          value=st.session_state.pack_header_data[
                                                                              "nome_tecnico"], key="pack_nome_tecnico")

    col_bat_num, col_term_id = st.columns(2)
    with col_bat_num:
        st.session_state.pack_header_data["numero_bateria"] = st.number_input(
            "Número da Bateria (Pack)",
            min_value=0,
            max_value=9999,
            value=st.session_state.pack_header_data["numero_bateria"],
            step=1, key="pack_num_bateria"
        )
    with col_term_id:
        st.session_state.pack_header_data["identificacao_terminais"] = st.text_input(
            "Identificação dos Terminais de Medição (ex: B0 a B12)",
            value=st.session_state.pack_header_data["identificacao_terminais"],
            help="Ex: B0 é o negativo da Célula 1, B1 é o positivo da Célula 1 / negativo da Célula 2, ..., B12 é o positivo da Célula 12.",
            key="pack_id_terminais"
        )

    st.divider()

    # --- Measurement Data Entry for Pack Tab ---
    st.header("Entrada de Dados de Medição do Pack")

    total_pack_voltage_input = st.number_input(
        f"**Tensão Total do Pack Medida (V)**",
        min_value=0.0,
        max_value=current_pack_specs_pack["PACK_TENSAO_MAX"] + 2.0,
        value=float(current_pack_specs_pack["PACK_TENSAO_MAX"]),
        step=0.01,  # Duas casas decimais
        format="%.2f",
        help=f"A tensão total deve estar entre {current_pack_specs_pack['PACK_TENSAO_CORTE']:.2f}V e {current_pack_specs_pack['PACK_TENSAO_MAX']:.2f}V. Idealmente, próximo a {current_pack_specs_pack['PACK_TENSAO_MAX']:.2f}V para análise de células em SoC alto.",
        key="pack_total_voltage"
    )

    st.markdown("---")
    st.subheader(
        f"Insira os valores medidos para cada uma das {current_pack_specs_pack['NUM_CELULAS_SERIE']} células do pack:")

    if 'pack_cell_data' not in st.session_state:
        st.session_state.pack_cell_data = [
            {"Célula": f"Célula {i + 1}", "OCV (V)": current_cell_specs_pack["TENSAO_CARGA_MAX"],
             "IR (mOhm)": current_cell_specs_pack["IR_NOVA_TIPICA"]}
            for i in range(current_pack_specs_pack["NUM_CELULAS_SERIE"])
        ]

    # Update default values if cell type changes
    if st.session_state.get('last_selected_cell_type_pack_for_defaults') != st.session_state.selected_cell_type_pack or \
            len(st.session_state.pack_cell_data) != current_pack_specs_pack["NUM_CELULAS_SERIE"]:
        st.session_state.pack_cell_data = [
            {"Célula": f"Célula {i + 1}", "OCV (V)": current_cell_specs_pack["TENSAO_CARGA_MAX"],
             "IR (mOhm)": current_cell_specs_pack["IR_NOVA_TIPICA"]}
            for i in range(current_pack_specs_pack["NUM_CELULAS_SERIE"])
        ]
        st.session_state.last_selected_cell_type_pack_for_defaults = st.session_state.selected_cell_type_pack

    df_pack_cells = pd.DataFrame(st.session_state.pack_cell_data)

    edited_df_pack_cells = st.data_editor(
        df_pack_cells,
        column_config={
            "Célula": st.column_config.Column("Célula", disabled=True),
            "OCV (V)": st.column_config.NumberColumn(
                "OCV (V)",
                min_value=0.0,
                max_value=current_cell_specs_pack["TENSAO_CARGA_MAX"] + 0.1,
                format="%.2f V",  # Duas casas decimais
                help="Tensão de Circuito Aberto da célula (Volts)"
            ),
            "IR (mOhm)": st.column_config.NumberColumn(
                "IR (mOhm)",
                min_value=0.0,
                max_value=100.0,  # Limite superior razoável para IR
                format="%.2f mOhm",  # Duas casas decimais
                help="Resistência Interna da célula (mili-Ohms)"
            ),
        },
        hide_index=True,
        num_rows="fixed",
        key="pack_cell_data_editor"
    )

    st.session_state.pack_cell_data = edited_df_pack_cells.to_dict('records')

    col_btn1, col_btn2 = st.columns([0.1, 0.9])
    with col_btn1:
        if st.button("Analisar Pack 🚀", type="primary", key="analisar_pack_btn"):
            st.session_state.run_pack_analysis = True
    with col_btn2:
        if st.button("Resetar Dados do Pack", key="resetar_pack_btn"):
            st.session_state.pack_header_data = {
                "data_teste": datetime.date.today(),
                "nome_tecnico": "Diógenes Oliveira",
                "numero_bateria": 0,
                "identificacao_terminais": "B0 a B12 (Célula 1: B0-B1, Célula 2: B1-B2, ..., Célula 12: B11-B12)"
            }
            st.session_state.pack_cell_data = [
                {"Célula": f"Célula {i + 1}", "OCV (V)": current_cell_specs_pack["TENSAO_CARGA_MAX"],
                 "IR (mOhm)": current_cell_specs_pack["IR_NOVA_TIPICA"]}
                for i in range(current_pack_specs_pack["NUM_CELULAS_SERIE"])
            ]
            total_pack_voltage_input = float(current_pack_specs_pack["PACK_TENSAO_MAX"])
            st.session_state.run_pack_analysis = False
            st.rerun()

    st.divider()

    # --- Analysis and Results for Pack Tab ---
    if st.session_state.get('run_pack_analysis', False):
        st.header("Resultados Detalhados da Análise do Pack")

        df_analise_pack = pd.DataFrame(st.session_state.pack_cell_data)
        df_analise_pack['OCV (V)'] = pd.to_numeric(df_analise_pack['OCV (V)'], errors='coerce')
        df_analise_pack['IR (mOhm)'] = pd.to_numeric(df_analise_pack['IR (mOhm)'], errors='coerce')

        if df_analise_pack[['OCV (V)', 'IR (mOhm)']].isnull().any().any():
            st.error(
                "Por favor, preencha todos os campos de OCV e IR com valores numéricos válidos antes de analisar o pack.")
        else:
            ocv_values_pack = df_analise_pack['OCV (V)'].tolist()
            ir_values_pack = df_analise_pack['IR (mOhm)'].tolist()

            ocv_media_pack = np.mean(ocv_values_pack)
            ir_media_pack = np.mean(ir_values_pack)
            ocv_soma_calculada_pack = np.sum(ocv_values_pack)

            # --- Avaliação da Tensão Total do Pack ---
            st.subheader("Análise da Tensão Total do Pack")
            st.metric(
                label="Tensão Total do Pack Medida (V)",
                value=f"{total_pack_voltage_input:.2f} V",
                delta=f"Esperado (soma OCVs): {ocv_soma_calculada_pack:.2f} V"
            )
            status_pack_v, motivos_pack_v = avaliar_pack_voltage(total_pack_voltage_input, current_pack_specs_pack)
            st.info(f"**Status da Tensão Total do Pack:** {status_pack_v} - {motivos_pack_v}")

            discrepancy_warning_text = None
            if abs(total_pack_voltage_input - ocv_soma_calculada_pack) > 0.5:  # Diferença maior que 0.5V
                discrepancy_warning_text = f"A tensão total medida ({total_pack_voltage_input:.2f}V) difere significativamente da soma das OCVs individuais ({ocv_soma_calculada_pack:.2f}V). Verifique as medições ou as conexões do pack (como soldas ponto)."
                st.warning(f"**Discrepância na Medição:** {discrepancy_warning_text}")

            st.markdown("---")

            # --- Avaliação Individual das Células ---
            resultados_celulas_pack = []
            status_pack_ordem = {"Bom": 0, "Monitorar": 1, "Ruim": 2, "Crítico": 3}
            status_pack_geral_base = status_pack_v  # Começa com o status da tensão total do pack

            for i, (ocv, ir) in enumerate(zip(ocv_values_pack, ir_values_pack)):
                status_celula, motivos_celula = avaliar_celula_individual(ocv, ir, current_cell_specs_pack,
                                                                          ocv_media_pack, ir_media_pack,
                                                                          is_avulsa=False)
                ocv_desvio = ocv - ocv_media_pack
                ir_desvio = ir - ir_media_pack

                if status_pack_ordem[status_celula] > status_pack_ordem[status_pack_geral_base]:
                    status_pack_geral_base = status_celula

                resultados_celulas_pack.append({
                    "Célula": f"Célula {i + 1}",
                    "OCV (V)": f"{ocv:.2f}",  # Duas casas decimais
                    "Desvio OCV (V)": f"{ocv_desvio:.2f}",  # Duas casas decimais
                    "IR (mOhm)": f"{ir:.2f}",  # Duas casas decimais
                    "Desvio IR (mOhm)": f"{ir_desvio:.2f}",  # Duas casas decimais
                    "Status": status_celula,
                    "Observações": motivos_celula
                })

            df_resultados_pack = pd.DataFrame(resultados_celulas_pack)

            st.subheader("Status Individual Detalhado das Células do Pack")
            st.dataframe(df_resultados_pack.style.applymap(color_status, subset=['Status']), use_container_width=True)

            st.markdown("---")

            st.subheader("Status Geral do Pack de Baterias (Combinado)")
            status_pack_alert_html_content = ""
            if status_pack_geral_base == "Bom":
                st.success(f"**Status Geral do Pack: {status_pack_geral_base}** ✅")
                st.write(
                    "Todas as células e a tensão total do pack estão dentro dos parâmetros esperados. A bateria está em excelente condição de uso.")
                status_pack_alert_html_content = f"<p style='color: green;'><strong>Status Geral do Pack: {status_pack_geral_base} ✅</strong></p><p>Todas as células e a tensão total do pack estão dentro dos parâmetros esperados. A bateria está em excelente condição de uso.</p>"
            elif status_pack_geral_base == "Monitorar":
                st.warning(f"**Status Geral do Pack: {status_pack_geral_base}** ⚠️")
                st.write(
                    "Algumas células ou a tensão total do pack exigem monitoramento. Recomenda-se verificar as observações individuais para cada célula e a tensão do pack. Considere uma recarga ou acompanhamento mais frequente.")
                status_pack_alert_html_content = f"<p style='color: orange;'><strong>Status Geral do Pack: {status_pack_geral_base} ⚠️</strong></p><p>Algumas células ou a tensão total do pack exigem monitoramento. Recomenda-se verificar as observações individuais para cada célula e a tensão do pack. Considere uma recarga ou acompanhamento mais frequente.</p>"
            elif status_pack_geral_base == "Ruim":
                st.error(f"**Status Geral do Pack: {status_pack_geral_base}** ❌")
                st.write(
                    "Foram identificadas células com degradação significativa, desequilíbrio acentuado ou a tensão total do pack está muito baixa. **Ações corretivas são urgentes.** A substituição das células problemáticas é fortemente recomendada para manter o desempenho, a eficiência e a segurança do pack.")
                status_pack_alert_html_content = f"<p style='color: red;'><strong>Status Geral do Pack: {status_pack_geral_base} ❌</strong></p><p>Foram identificadas células com degradação significativa, desequilíbrio acentuado ou a tensão total do pack está muito baixa. <strong>Ações corretivas são urgentes.</strong> A substituição das células problemáticas é fortemente recomendada para manter o desempenho, a eficiência e a segurança do pack.</p>"
            elif status_pack_geral_base == "Crítico":
                st.expander(
                    f"**Status Geral do Pack: {status_pack_geral_base}** 🚨 (Clique para ver detalhes do alerta)")
                st.markdown(
                    """
                    <div style="background-color: #ffdddd; padding: 15px; border-radius: 5px; border: 1px solid red;">
                        <h3>🚨 ALERTA DE SEGURANÇA CRÍTICO! 🚨</h3>
                        <p>Uma ou mais células estão em um estado crítico (tensão extremamente baixa/alta, IR perigosamente alta) OU a tensão total do pack está fora dos limites seguros.</p>
                        <p><b>Ações Imediatas Sugeridas:</b></p>
                        <ol>
                            <li><b>DESCONECTE IMEDIATAMENTE</b> qualquer conexão do pack para evitar maiores danos ou riscos.</li>
                            <li><b>NÃO TENTE CARREGAR OU DESCARREGAR</b> o pack neste estado.</li>
                            <li><b>ISOLAR</b> a bateria em uma área segura, longe de materiais inflamáveis e sob observação.</li>
                            <li><b>CONSIDERE SUBSTITUIÇÃO E DESCARTE SEGURO</b> de todo o pack ou das células críticas. Células de íon-lítio em estado crítico representam sério risco de incêndio, explosão ou vazamento químico.</li>
                            <li>Consulte um especialista em baterias ou o fabricante da ferramenta.</li>
                        </ol>
                    </div>
                    """, unsafe_allow_html=True
                )
                status_pack_alert_html_content = """
                <div style="background-color: #ffdddd; padding: 15px; border-radius: 5px; border: 1px solid red;">
                    <h3>🚨 ALERTA DE SEGURANÇA CRÍTICO! 🚨</h3>
                    <p>Uma ou mais células estão em um estado crítico (tensão extremamente baixa/alta, IR perigosamente alta) OU a tensão total do pack está fora dos limites seguros.</p>
                    <p><b>Ações Imediatas Sugeridas:</b></p>
                    <ol>
                        <li><b>DESCONECTE IMEDIATAMENTE</b> qualquer conexão do pack para evitar maiores danos ou riscos.</li>
                        <li><b>NÃO TENTE CARREGAR OU DESCARREGAR</b> o pack neste estado.</li>
                        <li><b>ISOLAR</b> a bateria em uma área segura, longe de materiais inflamáveis e sob observação.</li>
                        <li><b>CONSIDERE SUBSTITUIÇÃO E DESCARTE SEGURO</b> de todo o pack ou das células críticas. Células de íon-lítio em estado crítico representam sério risco de incêndio, explosão ou vazamento químico.</li>
                        <li>Consulte um especialista em baterias ou o fabricante da ferramenta.</li>
                    </ol>
                </div>
                """

            st.markdown("---")
            st.subheader("Estatísticas Consolidadas do Pack")
            col1, col2 = st.columns(2)
            with col1:
                st.metric("Média OCV por Célula", f"{ocv_media_pack:.2f} V")
            with col2:
                st.metric("Média IR por Célula", f"{ir_media_pack:.2f} mOhm")

            st.markdown("---")
            st.subheader("Visualização Rápida dos Dados do Pack")

            # Gráfico de OCVs Individuais
            fig_ocv_pack = px.bar(df_resultados_pack, x='Célula', y='OCV (V)',
                                  title='OCV por Célula no Pack',
                                  labels={'OCV (V)': 'OCV (V)'},
                                  color='Status',
                                  color_discrete_map={
                                      "Bom": "#ccffcc", "Monitorar": "#ffffcc",
                                      "Ruim": "#ffe6cc", "Crítico": "#ffcccc"
                                  },
                                  category_orders={"Célula": [f"Célula {i + 1}" for i in
                                                              range(current_pack_specs_pack["NUM_CELULAS_SERIE"])]})
            fig_ocv_pack.update_traces(marker_line_width=1, marker_line_color='black')
            fig_ocv_pack.add_hline(y=ocv_media_pack, line_dash="dot", line_color="gray", annotation_text="Média OCV")
            fig_ocv_pack.add_hline(y=current_cell_specs_pack["TENSAO_CARGA_MAX"], line_dash="dash", line_color="green",
                                   annotation_text=f"OCV Máx (Ideal: {current_cell_specs_pack['TENSAO_CARGA_MAX']:.2f}V)")
            fig_ocv_pack.add_hline(y=current_cell_specs_pack["TENSAO_CORTE"], line_dash="dash", line_color="red",
                                   annotation_text=f"OCV Mín (Corte: {current_cell_specs_pack['TENSAO_CORTE']:.2f}V)")
            st.plotly_chart(fig_ocv_pack, use_container_width=True)

            # Gráfico de IRs Individuais
            fig_ir_pack = px.bar(df_resultados_pack, x='Célula', y='IR (mOhm)',
                                 title='IR por Célula no Pack',
                                 labels={'IR (mOhm)': 'IR (mOhm)'},
                                 color='Status',
                                 color_discrete_map={
                                     "Bom": "#ccffcc", "Monitorar": "#ffffcc",
                                     "Ruim": "#ffe6cc", "Crítico": "#ffcccc"
                                 },
                                 category_orders={"Célula": [f"Célula {i + 1}" for i in
                                                             range(current_pack_specs_pack["NUM_CELULAS_SERIE"])]})
            fig_ir_pack.update_traces(marker_line_width=1, marker_line_color='black')
            fig_ir_pack.add_hline(y=ir_media_pack, line_dash="dot", line_color="gray", annotation_text="Média IR")
            fig_ir_pack.add_hline(y=current_cell_specs_pack["IR_NOVA_TIPICA"], line_dash="dash", line_color="green",
                                  annotation_text=f"IR Nova (Típica: {current_cell_specs_pack['IR_NOVA_TIPICA']:.2f}mΩ)")
            fig_ir_pack.add_hline(y=IR_LIMIAR_RUIM_MIN, line_dash="dash", line_color="red",
                                  annotation_text=f"IR Ruim (>{IR_LIMIAR_RUIM_MIN:.2f}mΩ)")
            st.plotly_chart(fig_ir_pack, use_container_width=True)

            st.markdown(
                """
                > **Importante:** Os limiares e as classificações são guias baseados em boas práticas e nas especificações da célula.
                > A criticidade exata e os limiares de aceitação podem precisar de **ajustes finos** com base na sua experiência com o equipamento,
                > nos ciclos de vida das baterias em sua fábrica e nas políticas de segurança específicas da sua indústria automotiva.
                > Mantenha um registro das medições ao longo do tempo para identificar tendências de degradação.
                """
            )

            st.markdown("---")
            st.subheader("Exportar Relatórios do Pack")

            # Prepare data for reports
            header_info_for_report_pack = {
                "data_teste": st.session_state.pack_header_data["data_teste"].strftime("%Y-%m-%d"),
                "nome_tecnico": st.session_state.pack_header_data["nome_tecnico"],
                "numero_bateria": st.session_state.pack_header_data["numero_bateria"],
                "identificacao_terminais": st.session_state.pack_header_data["identificacao_terminais"]
            }

            pack_voltage_info_for_report_pack = {
                "total_pack_voltage_medido": total_pack_voltage_input,
                "status_pack_v": status_pack_v,
                "motivos_pack_v": motivos_pack_v,
                "ocv_soma_calculada": ocv_soma_calculada_pack,
                "discrepancy_warning": discrepancy_warning_text
            }

            all_results_data_pack = {
                "header_info": header_info_for_report_pack,
                "cell_specs_individual": current_cell_specs_pack,
                "pack_specs_calculated": current_pack_specs_pack,
                "pack_voltage_analysis": pack_voltage_info_for_report_pack,
                "cell_level_summary": {
                    "ocv_media_pack": f"{ocv_media_pack:.2f} V",
                    "ir_media_pack": f"{ir_media_pack:.2f} mOhm",
                    "status_geral_pack": status_pack_geral_base
                },
                "cell_individual_results": df_resultados_pack.to_dict('records')
            }

            # Generate HTML report
            html_report_pack = generate_html_report_pack(
                header_info_for_report_pack,
                current_cell_specs_pack,
                pack_voltage_info_for_report_pack,
                df_resultados_pack,
                fig_ocv_pack.to_json(),
                fig_ir_pack.to_json(),
                status_pack_geral_base,
                status_pack_alert_html_content
            )
            st.download_button(
                label="Download Relatório HTML do Pack",
                data=html_report_pack,
                file_name=f"relatorio_pack_{st.session_state.pack_header_data['numero_bateria']}_{header_info_for_report_pack['data_teste']}.html",
                mime="text/html", key="download_html_pack"
            )

            # Generate JSON data
            json_data_pack = json.dumps(all_results_data_pack, indent=4, ensure_ascii=False)
            st.download_button(
                label="Download Dados JSON do Pack",
                data=json_data_pack,
                file_name=f"dados_pack_{st.session_state.pack_header_data['numero_bateria']}_{header_info_for_report_pack['data_teste']}.json",
                mime="application/json", key="download_json_pack"
            )

with tab2:
    st.title("🧪 Análise de Células Avulsas")

    # Cell Type Selection for Avulsa Tab
    if 'selected_cell_type_avulsa' not in st.session_state:
        st.session_state.selected_cell_type_avulsa = "Sony"

    st.session_state.selected_cell_type_avulsa = st.selectbox(
        "Selecione o tipo de célula avulsa:",
        list(ALL_CELL_SPECS.keys()),
        index=list(ALL_CELL_SPECS.keys()).index(st.session_state.selected_cell_type_avulsa),
        key="cell_type_selector_avulsa"
    )
    current_cell_specs_avulsa = ALL_CELL_SPECS[st.session_state.selected_cell_type_avulsa]

    st.markdown(
        f"""
        Esta aba permite registrar e analisar o desempenho de células **individuais e avulsas**
        do modelo **{current_cell_specs_avulsa['NOME']} ({current_cell_specs_avulsa['FABRICANTE']})**,
        que são utilizadas em ferramentas **{current_cell_specs_avulsa['FERRAMENTA_USO']}**.

        **Especificações da Célula ({current_cell_specs_avulsa['NOME']}):**
        - Capacidade Nominal: {current_cell_specs_avulsa['CAPACIDADE_NOMINAL_MAH']}mAh
        - Tensão Nominal (Individual): {current_cell_specs_avulsa['TENSAO_NOMINAL']:.2f}V
        - Tensão Máxima de Carga (Individual): {current_cell_specs_avulsa['TENSAO_CARGA_MAX']:.2f}V
        - Tensão de Corte de Descarga (Individual): {current_cell_specs_avulsa['TENSAO_CORTE']:.2f}V
        - Resistência Interna Típica (Nova): {current_cell_specs_avulsa['IR_NOVA_TIPICA']:.2f} mOhm
        - Corrente Máx. Contínua: {current_cell_specs_avulsa['CORRENTE_MAX_CONTINUA']:.2f}A

        **Procedimento de Teste Recomendado:**
        1.  Carregue as células em carregadores individuais até estarem totalmente carregadas (próximo de {current_cell_specs_avulsa['TENSAO_CARGA_MAX']:.2f}V).
        2.  Espere **30 minutos** após o carregamento para que a tensão se estabilize (repouso).
        3.  Anote um número de identificação exclusivo e a data diretamente na célula.
        4.  Meça a **Tensão de Circuito Aberto (OCV)** com um multímetro digital e a **Resistência Interna (IR)** com seu Fnirsi HRM-10.
        5.  Adicione as medições à tabela abaixo.
        """
    )
    st.divider()

    # --- Header Data Entry for Avulsa Tab ---
    st.header("Dados Gerais do Teste de Células Avulsas")

    if 'avulsa_header_data' not in st.session_state:
        st.session_state.avulsa_header_data = {
            "data_geracao_relatorio": datetime.date.today(),
            "nome_tecnico": "Diógenes Oliveira"
        }

    st.session_state.avulsa_header_data["nome_tecnico"] = st.text_input("Nome do Técnico Responsável",
                                                                        value=st.session_state.avulsa_header_data[
                                                                            "nome_tecnico"], key="avulsa_nome_tecnico")

    st.markdown("---")
    st.subheader("Adicionar Nova Célula Avulsa")

    # Initialize input values in session state for clearing and dynamic defaults
    if 'new_avulsa_id' not in st.session_state:
        st.session_state.new_avulsa_id = ""
    if 'new_avulsa_date' not in st.session_state or not isinstance(st.session_state.new_avulsa_date, datetime.date):
        st.session_state.new_avulsa_date = datetime.date.today()

    # Reset OCV/IR defaults based on current cell type selection
    if st.session_state.get(
            'last_selected_cell_type_avulsa_for_defaults') != st.session_state.selected_cell_type_avulsa:
        st.session_state.new_avulsa_ocv = current_cell_specs_avulsa["TENSAO_CARGA_MAX"]
        st.session_state.new_avulsa_ir = current_cell_specs_avulsa["IR_NOVA_TIPICA"]
        st.session_state.last_selected_cell_type_avulsa_for_defaults = st.session_state.selected_cell_type_avulsa
    # Ensure current defaults are used if not already set or changed by user
    if 'new_avulsa_ocv' not in st.session_state:  # Initial run without defaults
        st.session_state.new_avulsa_ocv = current_cell_specs_avulsa["TENSAO_CARGA_MAX"]
    if 'new_avulsa_ir' not in st.session_state:  # Initial run without defaults
        st.session_state.new_avulsa_ir = current_cell_specs_avulsa["IR_NOVA_TIPICA"]

    col_id, col_date_input = st.columns(2)
    with col_id:
        new_cell_id = st.text_input("ID da Célula", value=st.session_state.new_avulsa_id, key="input_new_avulsa_id")
    with col_date_input:
        new_cell_date = st.date_input("Data Teste", value=st.session_state.new_avulsa_date, key="input_new_avulsa_date")

    col_ocv_input, col_ir_input = st.columns(2)
    with col_ocv_input:
        new_cell_ocv = st.number_input(
            "OCV (V)",
            min_value=0.0,
            max_value=current_cell_specs_avulsa["TENSAO_CARGA_MAX"] + 0.1,
            value=st.session_state.new_avulsa_ocv,
            step=0.01,
            format="%.2f",
            key="input_new_avulsa_ocv"
        )
    with col_ir_input:
        new_cell_ir = st.number_input(
            "IR (mOhm)",
            min_value=0.0,
            max_value=100.0,
            value=st.session_state.new_avulsa_ir,
            step=0.01,
            format="%.2f",
            key="input_new_avulsa_ir"
        )

    if st.button("Adicionar Célula", key="add_avulsa_cell_btn"):
        if new_cell_id and new_cell_ocv is not None and new_cell_ir is not None:
            # Check for duplicate ID
            current_ids = [c['ID da Célula'] for c in st.session_state.get('avulsa_cell_data', [])]
            if new_cell_id in current_ids:
                st.warning(
                    f"ID da Célula '{new_cell_id}' já existe. Por favor, use um ID único ou edite a célula existente na tabela abaixo.")
            else:
                if 'avulsa_cell_data' not in st.session_state:
                    st.session_state.avulsa_cell_data = []
                st.session_state.avulsa_cell_data.append({
                    "ID da Célula": new_cell_id,
                    "Data Teste": new_cell_date.strftime("%Y-%m-%d"),  # Store as string
                    "OCV (V)": new_cell_ocv,
                    "IR (mOhm)": new_cell_ir,
                })
                # Clear inputs after adding
                st.session_state.new_avulsa_id = ""
                st.session_state.new_avulsa_date = datetime.date.today()
                st.session_state.new_avulsa_ocv = current_cell_specs_avulsa[
                    "TENSAO_CARGA_MAX"]  # Reset to current selected cell type default
                st.session_state.new_avulsa_ir = current_cell_specs_avulsa[
                    "IR_NOVA_TIPICA"]  # Reset to current selected cell type default
                st.rerun()  # Rerun to clear the input fields
        else:
            st.warning("Por favor, preencha todos os campos da nova célula.")

    st.markdown("---")
    st.subheader("Células Avulsas Cadastradas")

    if 'avulsa_cell_data' not in st.session_state or not st.session_state.avulsa_cell_data:
        st.info("Nenhuma célula avulsa cadastrada ainda. Use os campos acima para adicionar.")
        edited_df_avulsa_cells = pd.DataFrame([])
    else:
        # Convert the list of dicts into a DataFrame
        df_avulsa_cells_display = pd.DataFrame(st.session_state.avulsa_cell_data)

        # Robust conversion of 'Data Teste' column
        df_avulsa_cells_display['Data Teste'] = pd.to_datetime(df_avulsa_cells_display['Data Teste'], errors='coerce')
        df_avulsa_cells_display['Data Teste'] = df_avulsa_cells_display['Data Teste'].apply(
            lambda x: x.date() if pd.notna(x) else None)

        edited_df_avulsa_cells = st.data_editor(
            df_avulsa_cells_display,
            column_config={
                "ID da Célula": st.column_config.TextColumn("ID da Célula"),
                "Data Teste": st.column_config.DateColumn("Data Teste", format="DD/MM/YYYY"),
                # Now compatible with date objects
                "OCV (V)": st.column_config.NumberColumn("OCV (V)", format="%.2f V"),
                "IR (mOhm)": st.column_config.NumberColumn("IR (mOhm)", format="%.2f mOhm"),
            },
            hide_index=True,
            num_rows="dynamic",  # Still allow dynamic modification of existing rows
            key="avulsa_cell_data_display_editor"
        )
        # When updating the session state, convert date objects in DataFrame back to strings
        # for consistent storage in session state.
        updated_data_for_session = edited_df_avulsa_cells.to_dict('records')
        for item in updated_data_for_session:
            if isinstance(item['Data Teste'], datetime.date):
                item['Data Teste'] = item['Data Teste'].strftime("%Y-%m-%d")
            elif item['Data Teste'] is None:  # Handle None if user cleared a date field
                item['Data Teste'] = ""  # Store as empty string or None, depending on preference
        st.session_state.avulsa_cell_data = updated_data_for_session

    col_btn_avulsa1, col_btn_avulsa2 = st.columns([0.1, 0.9])
    with col_btn_avulsa1:
        if st.button("Analisar Células Avulsas 📊", type="primary", key="analisar_avulsas_btn"):
            if not st.session_state.avulsa_cell_data:
                st.warning("Nenhuma célula avulsa cadastrada para análise.")
            else:
                st.session_state.run_avulsa_analysis = True
    with col_btn_avulsa2:
        if st.button("Resetar Dados de Células Avulsas", key="resetar_avulsas_btn_main"):
            st.session_state.avulsa_header_data = {
                "data_geracao_relatorio": datetime.date.today(),
                "nome_tecnico": "Diógenes Oliveira"
            }
            st.session_state.avulsa_cell_data = []
            st.session_state.new_avulsa_id = ""  # Reset new cell input fields
            st.session_state.new_avulsa_date = datetime.date.today()  # Reset to datetime.date object
            st.session_state.new_avulsa_ocv = current_cell_specs_avulsa[
                "TENSAO_CARGA_MAX"]  # Reset to current selected cell type default
            st.session_state.new_avulsa_ir = current_cell_specs_avulsa[
                "IR_NOVA_TIPICA"]  # Reset to current selected cell type default
            st.session_state.run_avulsa_analysis = False

            # Reset JSON upload state as well
            st.session_state.uploaded_avulsa_json_string = None
            st.session_state.process_avulsa_json_flag = False

            st.rerun()

    st.divider()


    # --- JSON Upload Logic for Avulsa Tab ---
    # Function to handle file uploader changes
    def on_avulsa_json_upload_change():
        if st.session_state.avulsa_json_uploader is not None:
            # File has been uploaded. Store its content as a string for later processing.
            st.session_state.uploaded_avulsa_json_string = st.session_state.avulsa_json_uploader.getvalue().decode(
                "utf-8")
            st.session_state.process_avulsa_json_flag = True
        else:
            # Uploader was cleared. Reset processing state.
            st.session_state.uploaded_avulsa_json_string = None
            st.session_state.process_avulsa_json_flag = False


    # Initialize session state for JSON upload management
    if 'uploaded_avulsa_json_string' not in st.session_state:
        st.session_state.uploaded_avulsa_json_string = None
    if 'process_avulsa_json_flag' not in st.session_state:
        st.session_state.process_avulsa_json_flag = False

    st.subheader("Carregar Dados JSON de Células Avulsas")
    st.file_uploader(
        "Faça upload de um arquivo JSON (gerado por este aplicativo)",
        type="json",
        key="avulsa_json_uploader",
        on_change=on_avulsa_json_upload_change,
        help="Selecione um arquivo .json de relatório de células avulsas para carregar os dados na tabela."
    )

    # Logic to process the uploaded JSON, triggered by the flag set in the callback
    if st.session_state.process_avulsa_json_flag and st.session_state.uploaded_avulsa_json_string is not None:
        # Immediately reset the flag and clear the stored string to prevent re-processing in subsequent reruns
        st.session_state.process_avulsa_json_flag = False

        try:
            json_data = json.loads(st.session_state.uploaded_avulsa_json_string)
            if "individual_cell_results" in json_data:
                st.session_state.avulsa_cell_data = json_data["individual_cell_results"]

                if "header_info" in json_data:
                    st.session_state.avulsa_header_data["data_geracao_relatorio"] = json_data["header_info"].get(
                        "data_geracao_relatorio", datetime.date.today().strftime("%Y-%m-%d"))
                    st.session_state.avulsa_header_data["nome_tecnico"] = json_data["header_info"].get("nome_tecnico",
                                                                                                       "Diógenes Oliveira")

                if "cell_specs_individual" in json_data and "FABRICANTE" in json_data["cell_specs_individual"]:
                    cell_type_from_json = "Sony" if json_data["cell_specs_individual"][
                                                        "FABRICANTE"] == "Sony" else "Panasonic"
                    if cell_type_from_json != st.session_state.selected_cell_type_avulsa:
                        st.session_state.selected_cell_type_avulsa = cell_type_from_json
                        st.warning(
                            f"O tipo de célula foi automaticamente alterado para '{cell_type_from_json}' com base no arquivo JSON carregado.")

                st.success(
                    f"Dados de {len(st.session_state.avulsa_cell_data)} células carregados com sucesso do arquivo.")
                st.session_state.run_avulsa_analysis = False  # Ensure analysis is not run automatically just after upload

                st.session_state.uploaded_avulsa_json_string = None  # Clear after successful processing
                st.rerun()  # Trigger a rerun to fully update the UI with the new data.

            else:
                st.error("O arquivo JSON não contém a chave 'individual_cell_results' no formato esperado.")
                st.session_state.uploaded_avulsa_json_string = None  # Clear string even on error
        except json.JSONDecodeError:
            st.error("Erro ao decodificar o arquivo JSON. Certifique-se de que é um JSON válido.")
            st.session_state.uploaded_avulsa_json_string = None  # Clear string even on error
        except Exception as e:
            st.error(f"Ocorreu um erro inesperado ao carregar o arquivo: {e}")
            st.session_state.uploaded_avulsa_json_string = None  # Clear string even on error

    st.divider()

    # --- Analysis and Results for Avulsa Tab ---
    if st.session_state.get('run_avulsa_analysis', False) and st.session_state.avulsa_cell_data:
        st.header("Resultados da Análise de Células Avulsas")

        df_analise_avulsa = pd.DataFrame(st.session_state.avulsa_cell_data)  # Corrected variable name
        df_analise_avulsa['OCV (V)'] = pd.to_numeric(df_analise_avulsa['OCV (V)'], errors='coerce')
        df_analise_avulsa['IR (mOhm)'] = pd.to_numeric(df_analise_avulsa['IR (mOhm)'],
                                                       errors='coerce')  # Corrected variable name
        # Formata a coluna 'Data Teste' para string no formato YYYY-MM-DD para uso consistente
        # Isso já deve estar string YYYY-MM-DD se o session_state for bem gerenciado,
        # mas essa conversão garante que, para análise, sempre teremos a string formatada.
        df_analise_avulsa['Data Teste'] = pd.to_datetime(df_analise_avulsa['Data Teste'], errors='coerce').dt.strftime(
            '%Y-%m-%d')

        if df_analise_avulsa[['OCV (V)', 'IR (mOhm)']].isnull().any().any():
            st.error(
                "Por favor, preencha todos os campos de OCV e IR com valores numéricos válidos para as células avulsas.")
        else:
            resultados_celulas_avulsas = []
            for index, row in df_analise_avulsa.iterrows():
                # Para células avulsas, avaliamos sem a "média do pack" para desvio, focando nos limites absolutos
                status_celula, motivos_celula = avaliar_celula_individual(row['OCV (V)'], row['IR (mOhm)'],
                                                                          current_cell_specs_avulsa, is_avulsa=True)
                resultados_celulas_avulsas.append({
                    "ID da Célula": row['ID da Célula'],
                    "Data Teste": row['Data Teste'],
                    "OCV (V)": f"{row['OCV (V)']:.2f}",
                    "IR (mOhm)": f"{row['IR (mOhm)']:.2f}",
                    "Status": status_celula,
                    "Observações": motivos_celula
                })

            df_resultados_avulsas = pd.DataFrame(resultados_celulas_avulsas)

            # Ordenar da melhor para a pior: menor IR primeiro, depois maior OCV
            # Criar colunas temporárias para ordenação numérica
            df_resultados_avulsas['IR (mOhm)_float'] = pd.to_numeric(df_resultados_avulsas['IR (mOhm)'],
                                                                     errors='coerce')
            df_resultados_avulsas['OCV (V)_float'] = pd.to_numeric(df_resultados_avulsas['OCV (V)'], errors='coerce')

            df_resultados_avulsas = df_resultados_avulsas.sort_values(
                by=['IR (mOhm)_float', 'OCV (V)_float'],
                ascending=[True, False]  # IR ascendente (menor é melhor), OCV descendente (maior é melhor)
            ).drop(columns=['IR (mOhm)_float', 'OCV (V)_float'])  # Remover colunas temporárias

            st.subheader("Células Avulsas Analisadas (Ordenadas da Melhor para a Pior)")
            st.dataframe(df_resultados_avulsas.style.applymap(color_status, subset=['Status']),
                         use_container_width=True)

            st.markdown("---")
            st.subheader("Visualização Gráfica das Células Avulsas")

            # Gráfico de OCVs
            fig_ocv_avulsas = px.bar(df_resultados_avulsas, x='ID da Célula', y='OCV (V)',
                                     title='OCV por Célula Avulsa (Ordenado)',
                                     labels={'OCV (V)': 'OCV (V)'},
                                     color='Status',
                                     color_discrete_map={
                                         "Bom": "#ccffcc", "Monitorar": "#ffffcc",
                                         "Ruim": "#ffe6cc", "Crítico": "#ffcccc"
                                     },
                                     category_orders={"ID da Célula": df_resultados_avulsas[
                                         'ID da Célula'].tolist()})  # Garante a ordem do sort
            fig_ocv_avulsas.update_traces(marker_line_width=1, marker_line_color='black')
            fig_ocv_avulsas.add_hline(y=current_cell_specs_avulsa["TENSAO_CARGA_MAX"], line_dash="dash",
                                      line_color="green",
                                      annotation_text=f"OCV Máx ({current_cell_specs_avulsa['TENSAO_CARGA_MAX']:.2f}V)")
            fig_ocv_avulsas.add_hline(y=current_cell_specs_avulsa["TENSAO_CORTE"], line_dash="dash", line_color="red",
                                      annotation_text=f"OCV Mín ({current_cell_specs_avulsa['TENSAO_CORTE']:.2f}V)")
            st.plotly_chart(fig_ocv_avulsas, use_container_width=True)

            # Gráfico de IRs
            fig_ir_avulsas = px.bar(df_resultados_avulsas, x='ID da Célula', y='IR (mOhm)',
                                    title='IR por Célula Avulsa (Ordenado)',
                                    labels={'IR (mOhm)': 'IR (mOhm)'},
                                    color='Status',
                                    color_discrete_map={
                                        "Bom": "#ccffcc", "Monitorar": "#ffffcc",
                                        "Ruim": "#ffe6cc", "Crítico": "#ffcccc"
                                    },
                                    category_orders={"ID da Célula": df_resultados_avulsas[
                                        'ID da Célula'].tolist()})  # Garante a ordem do sort
            fig_ir_avulsas.update_traces(marker_line_width=1, marker_line_color='black')
            fig_ir_avulsas.add_hline(y=current_cell_specs_avulsa["IR_NOVA_TIPICA"], line_dash="dash",
                                     line_color="green",
                                     annotation_text=f"IR Nova (Típica: {current_cell_specs_avulsa['IR_NOVA_TIPICA']:.2f}mΩ)")
            fig_ir_avulsas.add_hline(y=IR_LIMIAR_RUIM_MIN, line_dash="dash", line_color="red",
                                     annotation_text=f"IR Ruim (>{IR_LIMIAR_RUIM_MIN:.2f}mΩ)")
            st.plotly_chart(fig_ir_avulsas, use_container_width=True)

            st.markdown("---")
            st.subheader("Exportar Relatórios de Células Avulsas")

            header_info_for_report_avulsas = {
                "data_geracao_relatorio": datetime.date.today().strftime("%Y-%m-%d"),
                "nome_tecnico": st.session_state.avulsa_header_data["nome_tecnico"]
            }

            # Generate HTML report
            html_report_avulsas = generate_html_report_avulsas(
                header_info_for_report_avulsas,
                current_cell_specs_avulsa,
                df_resultados_avulsas.copy(),  # Pass a copy to avoid modification issues
                fig_ocv_avulsas.to_json(),
                fig_ir_avulsas.to_json()
            )
            st.download_button(
                label="Download Relatório HTML de Células Avulsas",
                data=html_report_avulsas,
                file_name=f"relatorio_celulas_avulsas_{current_cell_specs_avulsa['NOME'].replace(' ', '_')}_{header_info_for_report_avulsas['data_geracao_relatorio']}.html",
                mime="text/html", key="download_html_avulsas"
            )

            # Generate JSON data
            all_results_data_avulsas = {
                "header_info": header_info_for_report_avulsas,
                "cell_specs_individual": current_cell_specs_avulsa,
                "individual_cell_results": df_resultados_avulsas.to_dict('records')
            }
            json_data_avulsas = json.dumps(all_results_data_avulsas, indent=4, ensure_ascii=False)
            st.download_button(
                label="Download Dados JSON de Células Avulsas",
                data=json_data_avulsas,
                file_name=f"dados_celulas_avulsas_{current_cell_specs_avulsa['NOME'].replace(' ', '_')}_{header_info_for_report_avulsas['data_geracao_relatorio']}.json",
                mime="application/json", key="download_json_avulsas"
            )
    elif st.session_state.get('run_avulsa_analysis', False):  # Only if analysis button was clicked but no data
        st.warning("Nenhuma célula avulsa cadastrada para análise. Por favor, adicione as medições na tabela acima.")