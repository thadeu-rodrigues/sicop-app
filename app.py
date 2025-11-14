import os
import requests
import urllib3
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from bs4 import BeautifulSoup
from datetime import datetime

# Desabilita avisos de SSL (Necessário para gov.br)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- CONFIGURAÇÃO DO BANCO ---
database_url = os.getenv('DATABASE_URL', 'sqlite:///sicop.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- MODELOS ATUALIZADOS ---
class Pregao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(50), unique=True, nullable=False)
    uasg = db.Column(db.String(20))
    data_importacao = db.Column(db.String(20))
    itens = db.relationship('Item', backref='pregao', lazy=True, cascade="all, delete-orphan")

class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero_item = db.Column(db.String(20)) 
    descricao = db.Column(db.Text, nullable=False)
    unidade = db.Column(db.String(20))
    quantidade = db.Column(db.String(20))
    
    # --- NOVOS CAMPOS DETALHADOS ---
    descricao_detalhada = db.Column(db.Text, default="")
    qtd_contratada = db.Column(db.String(20), default="-")
    qtd_empenhada = db.Column(db.String(20), default="-")
    saldo_contratacao = db.Column(db.String(20), default="-")
    
    pregao_id = db.Column(db.Integer, db.ForeignKey('pregao.id'), nullable=False)

with app.app_context():
    # ATENÇÃO: Se já tiver um banco criado, pode ser necessário deletá-lo
    # no Render ou localmente (sicop.db) para recriar com as colunas novas.
    db.create_all()

# --- FUNÇÃO AUXILIAR: PEGAR DETALHES DO ITEM ---
def fetch_item_details(codigo_item_srp):
    """
    Acessa a página de detalhes do item e extrai descrição longa e saldos.
    """
    try:
        url_detalhe = f"https://www2.comprasnet.gov.br/siasgnet-atasrp/public/visualizarItemSRP.do?method=iniciar&itemAtaSRP.codigoItemAtaSRP={codigo_item_srp}"
        headers = {'User-Agent': 'Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(url_detalhe, headers=headers, verify=False, timeout=10)
        
        if response.status_code != 200: return None

        soup = BeautifulSoup(response.text, 'html.parser')
        dados = {}

        # 1. Descrição Detalhada (Geralmente está num textarea ou div após o label)
        # Procura pelo texto e pega o próximo elemento
        label_desc = soup.find(string=lambda t: t and "Descrição Detalhada" in t)
        if label_desc:
            # Tenta achar o container próximo (geralmente um textarea ou div com class 'conteudo')
            container = label_desc.find_next(['textarea', 'div', 'span'])
            if container:
                dados['detalhada'] = container.get_text(strip=True)

        # 2. Quantidades (Contratada, Empenhada)
        # Procura a tabela ou campos de resumo
        # Baseado no print, parece um fieldset com labels e inputs disabled
        
        # Função helper para achar valor de input baseado no label anterior
        def get_val_by_label(text_label):
            lbl = soup.find(string=lambda t: t and text_label in t)
            if lbl:
                # Tenta achar input próximo
                inp = lbl.find_next('input')
                if inp: return inp.get('value', '').strip()
                # Ou tenta achar um span/div com valor
                spn = lbl.find_next(['span', 'div'])
                if spn: return spn.get_text(strip=True)
            return "-"

        # No SIASGnet, "Contratada" e "Empenhada" costumam estar numa tabela dentro de um fieldset
        # Vamos tentar uma busca genérica na tabela de "Resumo das quantidades"
        dados['contratada'] = get_val_by_label("Contratada")
        dados['empenhada'] = get_val_by_label("Empenhada")
        dados['saldo'] = get_val_by_label("Saldo para Contratação")

        # Limpeza: Se pegou o título da coluna em vez do valor, corrige
        if len(dados.get('contratada', '')) > 10: dados['contratada'] = '-'

        return dados

    except Exception as e:
        print(f"Erro ao detalhar item {codigo_item_srp}: {e}")
        return None

# --- ROTAS ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/importar', methods=['POST'])
def importar():
    data = request.get_json()
    numero_pregao = data.get('numero_pregao', '').strip()
    uasg = data.get('uasg', '').strip()
    url_alvo = data.get('url', '').strip()
    html_content = data.get('html', '')

    if not numero_pregao: return jsonify({"sucesso": False, "msg": "Nº Pregão obrigatório."})

    # 1. Obtém HTML Inicial
    if url_alvo:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36'}
            response = requests.get(url_alvo, headers=headers, verify=False, timeout=15)
            html_content = response.text
        except Exception as e:
            return jsonify({"sucesso": False, "msg": f"Erro URL: {str(e)}"})

    if not html_content: return jsonify({"sucesso": False, "msg": "HTML/URL vazio."})

    try:
        # Limpa anterior
        p = Pregao.query.filter_by(numero=numero_pregao).first()
        if p: db.session.delete(p); db.session.commit()

        novo = Pregao(numero=numero_pregao, uasg=uasg, data_importacao=datetime.now().strftime("%d/%m/%Y"))
        db.session.add(novo)

        soup = BeautifulSoup(html_content, 'html.parser')
        count = 0
        blacklist = ["PESQUISAR ITEM", "UASG GERENCIADORA", "DESCRIÇÃO DO ITEM", "MENU PRINCIPAL"]

        # Itera sobre as linhas da tabela PRINCIPAL
        for tabela in soup.find_all('table'):
            for row in tabela.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 2: continue
                
                txts = [c.get_text(strip=True) for c in cols]
                desc = max(txts, key=len).upper()
                
                if len(desc) < 4 or any(x in desc for x in blacklist): continue
                
                # Busca número do item
                num_item = "N/A"
                for x in txts[:3]:
                    if x.isdigit() and len(x) < 5: num_item = x; break
                
                if num_item == "N/A" and len(desc) < 10: continue

                # Unidade e Qtd Básica
                unid, qtd = "N/A", "N/A"
                for x in txts:
                    if x.upper() in ['UN', 'CX', 'KG', 'M', 'L', 'PAR', 'JG', 'FR']: unid = x.upper()
                    if x.replace('.','').isdigit() and len(x) < 9 and x != num_item: qtd = x

                # --- AQUI ESTÁ A MÁGICA DO DETALHAMENTO ---
                # Tenta achar o link para a página de detalhes (contém codigoItemAtaSRP)
                link_tag = row.find('a', href=True)
                desc_detalhada = ""
                q_contr = "-"
                q_emp = "-"
                q_saldo = "-"

                if link_tag and 'codigoItemAtaSRP=' in link_tag['href']:
                    # Extrai o código (ex: 45301647)
                    cod_srp = link_tag['href'].split('codigoItemAtaSRP=')[1].split('&')[0]
                    
                    # CHAMA O CRAWLER DE DETALHES
                    # (Nota: Isso deixa a importação mais lenta, mas traz os dados do print)
                    detalhes = fetch_item_details(cod_srp)
                    if detalhes:
                        desc_detalhada = detalhes.get('detalhada', '')
                        q_contr = detalhes.get('contratada', '-')
                        q_emp = detalhes.get('empenhada', '-')
                        q_saldo = detalhes.get('saldo', '-')

                # Salva
                db.session.add(Item(
                    numero_item=num_item, descricao=desc, unidade=unid, quantidade=qtd,
                    descricao_detalhada=desc_detalhada, # Novo
                    qtd_contratada=q_contr, # Novo
                    qtd_empenhada=q_emp,    # Novo
                    saldo_contratacao=q_saldo, # Novo
                    pregao=novo
                ))
                count += 1

        db.session.commit()
        return jsonify({"sucesso": True, "msg": f"Importado: {count} itens (Com detalhes completos)."})
    except Exception as e:
        db.session.rollback(); return jsonify({"sucesso": False, "msg": str(e)})

@app.route('/api/busca', methods=['GET'])
def busca():
    termo = request.args.get('q', '').upper()
    if not termo: return jsonify([])
    itens = Item.query.filter(Item.descricao.contains(termo)).limit(60).all()
    return jsonify([{
        "pregao_numero": i.pregao.numero, 
        "uasg": i.pregao.uasg, 
        "item_num": i.numero_item, 
        "descricao": i.descricao,
        # Enviamos os detalhes extras para o Frontend
        "descricao_detalhada": i.descricao_detalhada,
        "qtd_contratada": i.qtd_contratada,
        "qtd_empenhada": i.qtd_empenhada,
        "unidade": i.unidade
    } for i in itens])

# (As rotas de listar, detalhes e excluir continuam iguais às anteriores...)
@app.route('/api/excluir', methods=['POST'])
def excluir():
    data = request.get_json()
    p = Pregao.query.filter_by(numero=data.get('numero_pregao', '').strip()).first()
    if p: db.session.delete(p); db.session.commit(); return jsonify({"sucesso": True, "msg": "Excluído."})
    return jsonify({"sucesso": False, "msg": "Não encontrado."})

@app.route('/api/listar', methods=['GET'])
def listar():
    return jsonify([{"numero": p.numero, "uasg": p.uasg, "data": p.data_importacao, "qtd_itens": len(p.itens)} for p in Pregao.query.all()])

@app.route('/api/detalhes', methods=['GET'])
def detalhes():
    p = Pregao.query.filter_by(numero=request.args.get('numero')).first()
    if not p: return jsonify([])
    return jsonify([{
        "item_num": i.numero_item, 
        "descricao": i.descricao, 
        "unidade": i.unidade, 
        "quantidade": i.quantidade,
        "qtd_contratada": i.qtd_contratada,
        "qtd_empenhada": i.qtd_empenhada
    } for i in p.itens])

if __name__ == '__main__':
    app.run(debug=True)