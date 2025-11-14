import os
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from bs4 import BeautifulSoup
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURAÇÃO DO BANCO DE DADOS (Híbrida) ---
# Tenta pegar a URL do banco do Render. Se não achar, cria um arquivo local.
database_url = os.getenv('DATABASE_URL', 'sqlite:///sicop.db')

# Correção para compatibilidade do Render com SQLAlchemy
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- MODELOS (Tabelas) ---
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

# Cria as tabelas se não existirem
with app.app_context():
    db.create_all()

# --- ROTAS ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/importar', methods=['POST'])
def importar():
    data = request.get_json()
    html_content = data.get('html')
    numero_pregao = data.get('numero_pregao', '').strip()
    uasg = data.get('uasg', '').strip()

    if not html_content or not numero_pregao:
        return jsonify({"sucesso": False, "msg": "Dados incompletos."})

    try:
        # Atualização: Remove pregão antigo se existir
        pregao_existente = Pregao.query.filter_by(numero=numero_pregao).first()
        if pregao_existente:
            db.session.delete(pregao_existente)
            db.session.commit()

        novo_pregao = Pregao(
            numero=numero_pregao,
            uasg=uasg,
            data_importacao=datetime.now().strftime("%d/%m/%Y")
        )
        db.session.add(novo_pregao)

        # Scraping do HTML
        soup = BeautifulSoup(html_content, 'html.parser')
        count = 0
        
        for tabela in soup.find_all('table'):
            for linha in tabela.find_all('tr'):
                cols = linha.find_all('td')
                if len(cols) < 2: continue
                
                textos = [c.get_text(strip=True) for c in cols]
                
                # Lógica para encontrar a descrição
                descricao = max(textos, key=len)
                if len(descricao) < 4: continue

                # Tenta achar o número do item
                num_item = "N/A"
                for t in textos[:3]:
                    if t.isdigit(): 
                        num_item = t
                        break
                
                # Tenta achar unidade e qtd
                unid = "N/A"
                qtd = "N/A"
                for t in textos:
                    if t.upper() in ['UN', 'CX', 'KG', 'M', 'L', 'PAR', 'JG', 'FR']: unid = t
                    if t.replace('.', '').isdigit() and len(t) < 8 and t != num_item: qtd = t

                item_obj = Item(
                    numero_item=num_item,
                    descricao=descricao.upper(),
                    unidade=unid,
                    quantidade=qtd,
                    pregao=novo_pregao
                )
                db.session.add(item_obj)
                count += 1

        db.session.commit()
        return jsonify({"sucesso": True, "msg": f"Sucesso! {count} itens importados."})

    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "msg": f"Erro no servidor: {str(e)}"})

@app.route('/api/busca', methods=['GET'])
def busca():
    termo = request.args.get('q', '').upper()
    if not termo: return jsonify([])

    # Busca limitando a 60 resultados para performance
    itens = Item.query.filter(Item.descricao.contains(termo)).limit(60).all()
    
    resultados = []
    for item in itens:
        resultados.append({
            "pregao_numero": item.pregao.numero,
            "uasg": item.pregao.uasg,
            "item_num": item.numero_item,
            "descricao": item.descricao,
            "unidade": item.unidade
        })

    return jsonify(resultados)

if __name__ == '__main__':
    app.run(debug=True)