import os
import requests
import urllib3
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from bs4 import BeautifulSoup
from datetime import datetime

# Desabilita avisos de SSL inseguro
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# --- CONFIGURAÇÃO DO BANCO ---
database_url = os.getenv('DATABASE_URL', 'sqlite:///sicop.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- MODELOS ---
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
    pregao_id = db.Column(db.Integer, db.ForeignKey('pregao.id'), nullable=False)

with app.app_context():
    db.create_all()

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

    if not numero_pregao:
        return jsonify({"sucesso": False, "msg": "O número do pregão é obrigatório."})

    if url_alvo:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36'}
            response = requests.get(url_alvo, headers=headers, verify=False, timeout=15)
            response.raise_for_status()
            html_content = response.text
        except Exception as e:
            return jsonify({"sucesso": False, "msg": f"Erro na URL: {str(e)}. Use o HTML manual."})

    if not html_content:
        return jsonify({"sucesso": False, "msg": "HTML/URL vazios."})

    try:
        pregao_existente = Pregao.query.filter_by(numero=numero_pregao).first()
        if pregao_existente:
            db.session.delete(pregao_existente)
            db.session.commit()

        novo_pregao = Pregao(numero=numero_pregao, uasg=uasg, data_importacao=datetime.now().strftime("%d/%m/%Y"))
        db.session.add(novo_pregao)

        soup = BeautifulSoup(html_content, 'html.parser')
        count = 0
        
        for tabela in soup.find_all('table'):
            for linha in tabela.find_all('tr'):
                cols = linha.find_all('td')
                if len(cols) < 2: continue
                textos = [c.get_text(strip=True) for c in cols]
                descricao = max(textos, key=len)
                if len(descricao) < 4: continue
                
                num_item = "N/A"
                for t in textos[:3]:
                    if t.isdigit(): num_item = t; break
                
                unid = "N/A"; qtd = "N/A"
                for t in textos:
                    if t.upper() in ['UN', 'CX', 'KG', 'M', 'L', 'PAR', 'JG', 'FR']: unid = t
                    if t.replace('.', '').isdigit() and len(t) < 8 and t != num_item: qtd = t

                db.session.add(Item(numero_item=num_item, descricao=descricao.upper(), unidade=unid, quantidade=qtd, pregao=novo_pregao))
                count += 1

        db.session.commit()
        origem = "via URL" if url_alvo else "via HTML"
        return jsonify({"sucesso": True, "msg": f"Sucesso! {count} itens importados ({origem})."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "msg": str(e)})

@app.route('/api/busca', methods=['GET'])
def busca():
    termo = request.args.get('q', '').upper()
    if not termo: return jsonify([])
    itens = Item.query.filter(Item.descricao.contains(termo)).limit(60).all()
    res = []
    for i in itens:
        res.append({"pregao_numero": i.pregao.numero, "uasg": i.pregao.uasg, "item_num": i.numero_item, "descricao": i.descricao, "unidade": i.unidade})
    return jsonify(res)

@app.route('/api/excluir', methods=['POST'])
def excluir():
    data = request.get_json()
    numero = data.get('numero_pregao', '').strip()
    pregao = Pregao.query.filter_by(numero=numero).first()
    if pregao:
        db.session.delete(pregao)
        db.session.commit()
        return jsonify({"sucesso": True, "msg": "Pregão excluído."})
    return jsonify({"sucesso": False, "msg": "Pregão não encontrado."})

# --- NOVAS ROTAS PARA LISTAGEM ---

@app.route('/api/listar', methods=['GET'])
def listar_pregoes():
    pregoes = Pregao.query.all()
    lista = []
    for p in pregoes:
        lista.append({
            "numero": p.numero,
            "uasg": p.uasg,
            "data": p.data_importacao,
            "qtd_itens": len(p.itens)
        })
    return jsonify(lista)

@app.route('/api/detalhes', methods=['GET'])
def detalhes_pregao():
    numero = request.args.get('numero')
    pregao = Pregao.query.filter_by(numero=numero).first()
    if not pregao: return jsonify([])
    
    itens = []
    for i in pregao.itens:
        itens.append({
            "item_num": i.numero_item,
            "descricao": i.descricao,
            "unidade": i.unidade,
            "quantidade": i.quantidade
        })
    return jsonify(itens)

if __name__ == '__main__':
    app.run(debug=True)