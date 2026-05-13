import os, csv, io, json, base64, urllib.request, re, smtplib, mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from werkzeug.utils import secure_filename

import qrcode
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, g, Response, send_file, abort)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import sqlite3

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))
app.config['WTF_CSRF_ENABLED'] = True
csrf = CSRFProtect(app)

DB           = os.environ.get('DB_PATH', 'seleccion.db')
OLLAMA_URL   = os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate')
UPLOADS_DIR  = Path(os.environ.get('UPLOADS_DIR', 'uploads'))
UPLOADS_DIR.mkdir(exist_ok=True)
EXTENSIONES_OK = {'.pdf', '.jpg', '.jpeg', '.png', '.webp'}
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'llama3')
PER_PAGE     = 25


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def init_db():
    conn = sqlite3.connect(DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cursos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            codigo TEXT,
            fecha_inicio TEXT,
            fecha_fin TEXT,
            plazas INTEGER DEFAULT 20,
            descripcion TEXT,
            preguntas_prueba TEXT DEFAULT '[]',
            archivado INTEGER DEFAULT 0,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS alumnos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            curso_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            apellidos TEXT NOT NULL,
            fecha_nacimiento TEXT, domicilio TEXT,
            numero TEXT, puerta TEXT, poblacion TEXT, cp TEXT,
            telefono TEXT, telefono_emergencias TEXT,
            email TEXT, edad TEXT, sexo TEXT,
            nacionalidad TEXT, dni TEXT, seg_social TEXT, expediente TEXT,
            fecha_inscripcion_servef TEXT, oficina_servef TEXT,
            cobra_prestaciones TEXT, situacion_laboral TEXT,
            titulacion TEXT, titulacion_especialidad TEXT,
            estudia_actualmente TEXT, que_estudia TEXT,
            cursos_fpo TEXT DEFAULT '[]',
            experiencia TEXT DEFAULT '[]',
            desea_realizar TEXT, motivacion TEXT,
            tiene_carnet TEXT, tiene_vehiculo TEXT,
            razon_interes TEXT, razon_motivacion TEXT,
            razon_empleo TEXT, razon_impedimentos TEXT,
            razon_limitaciones TEXT, observaciones TEXT,
            acepta_compromiso TEXT,
            respuestas_prueba TEXT DEFAULT '{}',
            estado TEXT DEFAULT 'pendiente',
            puntuacion_ia REAL, analisis_ia TEXT,
            notas TEXT DEFAULT '',
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS documentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            nombre_original TEXT NOT NULL,
            nombre_fichero TEXT NOT NULL,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (alumno_id) REFERENCES alumnos(id)
        );
        CREATE TABLE IF NOT EXISTS historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alumno_id INTEGER NOT NULL,
            curso_id INTEGER NOT NULL,
            usuario_nombre TEXT,
            accion TEXT NOT NULL,
            detalle TEXT,
            creado_en TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    for sql in [
        "ALTER TABLE cursos ADD COLUMN archivado INTEGER DEFAULT 0",
        "ALTER TABLE cursos ADD COLUMN inscripciones_abiertas INTEGER DEFAULT 1",
        "ALTER TABLE alumnos ADD COLUMN notas TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass
    conn.close()


# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        conn = get_db()
        user = conn.execute('SELECT * FROM usuarios WHERE email=?',
                            (request.form['email'],)).fetchone()
        if user and check_password_hash(user['password'], request.form['password']):
            session['user_id']     = user['id']
            session['user_nombre'] = user['nombre']
            return redirect(url_for('dashboard'))
        error = 'Email o contraseña incorrectos.'
    return render_template('login.html', error=error)


@app.route('/registro', methods=['GET', 'POST'])
def registro():
    error = None
    if request.method == 'POST':
        conn = get_db()
        try:
            conn.execute('INSERT INTO usuarios (nombre,email,password) VALUES (?,?,?)',
                         (request.form['nombre'], request.form['email'],
                          generate_password_hash(request.form['password'])))
            conn.commit()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            error = 'Este email ya está registrado.'
    return render_template('registro.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── DASHBOARD ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    tab       = request.args.get('tab', 'activos')
    archivado = 1 if tab == 'archivados' else 0
    conn      = get_db()
    cursos    = conn.execute("""
        SELECT c.*,
               COUNT(a.id) as total,
               SUM(CASE WHEN a.estado='seleccionado'    THEN 1 ELSE 0 END) as seleccionados,
               SUM(CASE WHEN a.estado='reserva'         THEN 1 ELSE 0 END) as reservas,
               SUM(CASE WHEN a.estado='no_seleccionado' THEN 1 ELSE 0 END) as no_seleccionados,
               SUM(CASE WHEN a.estado='pendiente'       THEN 1 ELSE 0 END) as pendientes
        FROM cursos c LEFT JOIN alumnos a ON a.curso_id=c.id
        WHERE c.archivado=?
        GROUP BY c.id ORDER BY c.creado_en DESC
    """, (archivado,)).fetchall()
    return render_template('dashboard.html', cursos=cursos, tab=tab)


# ── CURSOS ────────────────────────────────────────────────────────────────────

@app.route('/cursos/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_curso():
    if request.method == 'POST':
        preguntas, i = [], 0
        while True:
            etiqueta = request.form.get(f'preg_label_{i}', '').strip()
            if not etiqueta:
                break
            tipo     = request.form.get(f'preg_tipo_{i}', 'texto')
            opciones = []
            if tipo == 'multiple':
                opciones = [o.strip() for o in
                            request.form.get(f'preg_opts_{i}', '').split('|') if o.strip()]
            preguntas.append({'label': etiqueta, 'tipo': tipo, 'opciones': opciones})
            i += 1
        conn = get_db()
        conn.execute(
            'INSERT INTO cursos (nombre,codigo,fecha_inicio,fecha_fin,plazas,descripcion,preguntas_prueba)'
            ' VALUES (?,?,?,?,?,?,?)',
            (request.form['nombre'], request.form.get('codigo', ''),
             request.form.get('fecha_inicio', ''), request.form.get('fecha_fin', ''),
             request.form.get('plazas', 20), request.form.get('descripcion', ''),
             json.dumps(preguntas, ensure_ascii=False)))
        conn.commit()
        return redirect(url_for('dashboard'))
    return render_template('nuevo_curso.html')


@app.route('/cursos/<int:cid>')
@login_required
def ver_curso(cid):
    conn  = get_db()
    curso = conn.execute('SELECT * FROM cursos WHERE id=?', (cid,)).fetchone()
    if not curso:
        return 'Curso no encontrado', 404

    q      = request.args.get('q', '').strip()
    estado = request.args.get('estado', '')
    page   = max(1, int(request.args.get('page', 1)))

    params  = [cid]
    filtros = "WHERE a.curso_id=?"
    if q:
        filtros += " AND (a.nombre||' '||a.apellidos LIKE ? OR a.dni LIKE ? OR a.email LIKE ?)"
        params  += [f'%{q}%', f'%{q}%', f'%{q}%']
    if estado:
        filtros += " AND a.estado=?"
        params.append(estado)

    total_rows = conn.execute(
        f"SELECT COUNT(*) FROM alumnos a {filtros}", params).fetchone()[0]
    total_pags = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
    page       = min(page, total_pags)
    offset     = (page - 1) * PER_PAGE

    alumnos = conn.execute(f"""
        SELECT a.* FROM alumnos a {filtros}
        ORDER BY CASE a.estado
            WHEN 'seleccionado'    THEN 1
            WHEN 'reserva'         THEN 2
            WHEN 'pendiente'       THEN 3
            WHEN 'no_seleccionado' THEN 4
        END, a.puntuacion_ia DESC
        LIMIT ? OFFSET ?
    """, params + [PER_PAGE, offset]).fetchall()

    stats = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN estado='seleccionado'    THEN 1 ELSE 0 END) as seleccionados,
               SUM(CASE WHEN estado='reserva'         THEN 1 ELSE 0 END) as reservas,
               SUM(CASE WHEN estado='pendiente'       THEN 1 ELSE 0 END) as pendientes,
               SUM(CASE WHEN estado='no_seleccionado' THEN 1 ELSE 0 END) as no_sel
        FROM alumnos WHERE curso_id=?
    """, (cid,)).fetchone()

    preguntas       = json.loads(curso['preguntas_prueba'] or '[]')
    plazas_llenas   = (stats['seleccionados'] or 0) >= (curso['plazas'] or 0)
    inscripcion_url = request.host_url.rstrip('/') + url_for('inscripcion', cid=cid)
    qr_b64          = _generar_qr(inscripcion_url)

    return render_template('ver_curso.html',
                           curso=curso, alumnos=alumnos, preguntas=preguntas,
                           stats=stats, plazas_llenas=plazas_llenas,
                           inscripcion_url=inscripcion_url, qr_b64=qr_b64,
                           q=q, estado_filtro=estado,
                           page=page, total_pags=total_pags, total_rows=total_rows)


@app.route('/cursos/<int:cid>/eliminar', methods=['POST'])
@login_required
def eliminar_curso(cid):
    conn = get_db()
    conn.execute('DELETE FROM alumnos WHERE curso_id=?', (cid,))
    conn.execute('DELETE FROM cursos WHERE id=?', (cid,))
    conn.commit()
    return redirect(url_for('dashboard'))


@app.route('/cursos/<int:cid>/archivar', methods=['POST'])
@login_required
def archivar_curso(cid):
    conn = get_db()
    conn.execute('UPDATE cursos SET archivado=1 WHERE id=?', (cid,))
    conn.commit()
    return redirect(url_for('dashboard'))


@app.route('/cursos/<int:cid>/restaurar', methods=['POST'])
@login_required
def restaurar_curso(cid):
    conn = get_db()
    conn.execute('UPDATE cursos SET archivado=0 WHERE id=?', (cid,))
    conn.commit()
    return redirect(url_for('dashboard', tab='archivados'))


@app.route('/cursos/<int:cid>/duplicar', methods=['POST'])
@login_required
def duplicar_curso(cid):
    conn  = get_db()
    curso = conn.execute('SELECT * FROM cursos WHERE id=?', (cid,)).fetchone()
    if not curso:
        return redirect(url_for('dashboard'))
    conn.execute(
        'INSERT INTO cursos (nombre,codigo,plazas,descripcion,preguntas_prueba) VALUES (?,?,?,?,?)',
        (f"[Copia] {curso['nombre']}", curso['codigo'],
         curso['plazas'], curso['descripcion'], curso['preguntas_prueba']))
    conn.commit()
    return redirect(url_for('dashboard'))


@app.route('/cursos/<int:cid>/exportar-csv')
@login_required
def exportar_csv(cid):
    conn  = get_db()
    curso = conn.execute('SELECT * FROM cursos WHERE id=?', (cid,)).fetchone()
    if not curso:
        return 'No encontrado', 404
    alumnos = conn.execute(
        'SELECT * FROM alumnos WHERE curso_id=? ORDER BY puntuacion_ia DESC NULLS LAST, creado_en',
        (cid,)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Posición', 'Nombre', 'Apellidos', 'DNI', 'Email', 'Teléfono',
        'Situación laboral', 'Cobra prestaciones', 'Titulación',
        'Meses experiencia', 'Estado', 'Puntuación IA', 'Notas'
    ])
    for i, a in enumerate(alumnos, 1):
        exp   = json.loads(a['experiencia'] or '[]')
        meses = sum(int(e.get('anios', 0) or 0) * 12 + int(e.get('meses', 0) or 0)
                    for e in exp)
        writer.writerow([
            i, a['nombre'], a['apellidos'], a['dni'] or '', a['email'] or '',
            a['telefono'] or '', a['situacion_laboral'] or '',
            a['cobra_prestaciones'] or '', a['titulacion'] or '',
            meses, a['estado'], a['puntuacion_ia'] or '', a['notas'] or ''
        ])

    nombre_f = f"alumnos_{curso['nombre'].replace(' ', '_')[:40]}.csv"
    return Response(
        '﻿' + output.getvalue(),
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="{nombre_f}"'}
    )


@app.route('/cursos/<int:cid>/toggle-inscripciones', methods=['POST'])
@login_required
def toggle_inscripciones(cid):
    conn   = get_db()
    curso  = conn.execute('SELECT inscripciones_abiertas FROM cursos WHERE id=?', (cid,)).fetchone()
    nuevo  = 0 if (curso['inscripciones_abiertas'] or 1) else 1
    conn.execute('UPDATE cursos SET inscripciones_abiertas=? WHERE id=?', (nuevo, cid))
    conn.commit()
    return redirect(url_for('ver_curso', cid=cid))


@app.route('/cursos/<int:cid>/estado-masivo', methods=['POST'])
@login_required
def estado_masivo(cid):
    ids    = request.form.getlist('alumno_ids')
    estado = request.form.get('nuevo_estado', '')
    if not ids or estado not in ('pendiente', 'seleccionado', 'reserva', 'no_seleccionado'):
        return redirect(url_for('ver_curso', cid=cid))
    conn = get_db()
    for aid in ids:
        conn.execute('UPDATE alumnos SET estado=? WHERE id=? AND curso_id=?',
                     (estado, int(aid), cid))
        _log_historial(conn, int(aid), cid, session.get('user_nombre', ''),
                       'Estado masivo', estado)
    conn.commit()
    return redirect(url_for('ver_curso', cid=cid))


@app.route('/cursos/<int:cid>/exportar-excel')
@login_required
def exportar_excel(cid):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    conn  = get_db()
    curso = conn.execute('SELECT * FROM cursos WHERE id=?', (cid,)).fetchone()
    if not curso:
        return 'No encontrado', 404
    alumnos = conn.execute(
        "SELECT * FROM alumnos WHERE curso_id=? ORDER BY CASE estado "
        "WHEN 'seleccionado' THEN 1 WHEN 'reserva' THEN 2 "
        "WHEN 'pendiente' THEN 3 ELSE 4 END, puntuacion_ia DESC NULLS LAST",
        (cid,)).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Alumnos'

    azul   = '3d5f6e'
    dorado = 'b5a050'
    cols   = ['Pos.', 'Nombre', 'Apellidos', 'DNI', 'Email', 'Teléfono',
              'Situación laboral', 'Cobra prestaciones', 'Titulación',
              'Meses experiencia', 'Estado', 'Punt. IA', 'Expediente', 'Notas']

    hdr_font  = Font(bold=True, color='FFFFFF', name='Calibri', size=10)
    hdr_fill  = PatternFill('solid', fgColor=azul)
    hdr_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    thin      = Side(style='thin', color='d0d0d0')
    border    = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.append(cols)
    for cell in ws[1]:
        cell.font = hdr_font; cell.fill = hdr_fill
        cell.alignment = hdr_align; cell.border = border
    ws.row_dimensions[1].height = 28

    fill_sel  = PatternFill('solid', fgColor='e8f4ec')
    fill_res  = PatternFill('solid', fgColor='fef6e4')
    fill_no   = PatternFill('solid', fgColor='faeaea')
    fill_alt  = PatternFill('solid', fgColor='f7f7f5')
    estado_fills = {'seleccionado': fill_sel, 'reserva': fill_res,
                    'no_seleccionado': fill_no}

    for i, a in enumerate(alumnos, 1):
        exp   = json.loads(a['experiencia'] or '[]')
        meses = sum(int(e.get('anios', 0) or 0) * 12 + int(e.get('meses', 0) or 0)
                    for e in exp)
        row = [i, a['nombre'], a['apellidos'], a['dni'] or '',
               a['email'] or '', a['telefono'] or '',
               a['situacion_laboral'] or '', a['cobra_prestaciones'] or '',
               a['titulacion'] or '', meses, a['estado'],
               a['puntuacion_ia'] or '', a['expediente'] or '', a['notas'] or '']
        ws.append(row)
        fill = estado_fills.get(a['estado'], fill_alt if i % 2 == 0 else None)
        for cell in ws[i + 1]:
            if fill:
                cell.fill = fill
            cell.border = border
            cell.alignment = Alignment(vertical='center')

    anchos = [5, 16, 20, 12, 28, 13, 25, 10, 22, 8, 14, 8, 14, 30]
    for col_i, ancho in enumerate(anchos, 1):
        ws.column_dimensions[get_column_letter(col_i)].width = ancho
    ws.freeze_panes = 'A2'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nombre_f = f"alumnos_{curso['nombre'].replace(' ', '_')[:40]}.xlsx"
    return Response(buf.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{nombre_f}"'})


@app.route('/buscar')
@login_required
def buscar_global():
    q = request.args.get('q', '').strip()
    resultados = []
    if q:
        conn = get_db()
        resultados = conn.execute("""
            SELECT a.id, a.nombre, a.apellidos, a.dni, a.email, a.estado,
                   c.id as curso_id, c.nombre as curso_nombre
            FROM alumnos a JOIN cursos c ON c.id=a.curso_id
            WHERE a.nombre||' '||a.apellidos LIKE ?
               OR a.dni LIKE ? OR a.email LIKE ?
            ORDER BY a.apellidos, a.nombre
            LIMIT 60
        """, (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
    return render_template('buscar.html', q=q, resultados=resultados)


# ── INSCRIPCIÓN PÚBLICA ───────────────────────────────────────────────────────

@app.route('/inscripcion/<int:cid>', methods=['GET', 'POST'])
def inscripcion(cid):
    conn  = get_db()
    curso = conn.execute('SELECT * FROM cursos WHERE id=?', (cid,)).fetchone()
    if not curso:
        return 'Curso no encontrado', 404
    if not (curso['inscripciones_abiertas'] if 'inscripciones_abiertas' in curso.keys() else 1):
        return render_template('inscripcion_cerrada.html', curso=curso)
    preguntas = json.loads(curso['preguntas_prueba'] or '[]')

    if request.method == 'POST':
        f      = request.form
        errors = []

        obligatorios = [
            ('nombre',                  'El nombre es obligatorio.'),
            ('apellidos',               'Los apellidos son obligatorios.'),
            ('fecha_nacimiento',        'La fecha de nacimiento es obligatoria.'),
            ('edad',                    'La edad es obligatoria.'),
            ('dni',                     'El DNI/NIE es obligatorio.'),
            ('seg_social',              'El número de Seguridad Social es obligatorio.'),
            ('nacionalidad',            'La nacionalidad es obligatoria.'),
            ('domicilio',               'El domicilio es obligatorio.'),
            ('poblacion',               'La población es obligatoria.'),
            ('cp',                      'El código postal es obligatorio.'),
            ('telefono',                'El teléfono es obligatorio.'),
            ('fecha_inscripcion_servef','La fecha de inscripción en SERVEF/Labora es obligatoria.'),
            ('oficina_servef',          'La oficina SERVEF/Labora es obligatoria.'),
            ('motivacion',              'La motivación para el curso es obligatoria.'),
            ('razon_interes',           'Debe responder a la pregunta 1 del cuestionario.'),
            ('razon_motivacion',        'Debe responder a la pregunta 2 del cuestionario.'),
            ('razon_empleo',            'Debe responder a la pregunta 3 del cuestionario.'),
            ('razon_impedimentos',      'Debe responder a la pregunta 4 del cuestionario.'),
            ('razon_limitaciones',      'Debe responder a la pregunta 5 del cuestionario.'),
        ]
        for campo, msg in obligatorios:
            if not f.get(campo, '').strip():
                errors.append(msg)

        radio_obligatorios = [
            ('sexo',                'Sexo'),
            ('cobra_prestaciones',  'Cobra prestaciones'),
            ('situacion_laboral',   'Situación laboral'),
            ('titulacion',          'Nivel de estudios'),
            ('estudia_actualmente', '¿Estudia actualmente?'),
            ('desea_realizar',      '¿Desea realizar el curso?'),
            ('tiene_carnet',        '¿Tiene carnet de conducir?'),
            ('tiene_vehiculo',      '¿Dispone de vehículo?'),
        ]
        for campo, label in radio_obligatorios:
            if not f.get(campo, '').strip():
                errors.append(f'Debe seleccionar una opción en: {label}.')

        email = f.get('email', '').strip()
        if not email:
            errors.append('El correo electrónico es obligatorio.')
        elif not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            errors.append('El formato del email no es válido.')
        if not f.get('acepta_compromiso'):
            errors.append('Debe aceptar las condiciones de Labora para continuar.')

        if errors:
            return render_template('inscripcion.html', curso=curso,
                                   preguntas=preguntas, errors=errors, form=f)

        fpo, exp = [], []
        for i in range(1, 4):
            e = f.get(f'fpo_esp_{i}', '').strip()
            if e:
                fpo.append({'especialidad': e, 'horas': f.get(f'fpo_h_{i}', ''),
                            'anyo': f.get(f'fpo_a_{i}', '')})
        for i in range(1, 4):
            p = f.get(f'exp_p_{i}', '').strip()
            if p:
                exp.append({'profesion': p, 'categoria': f.get(f'exp_c_{i}', ''),
                            'anios': f.get(f'exp_a_{i}', ''), 'meses': f.get(f'exp_m_{i}', ''),
                            'empresa': f.get(f'exp_e_{i}', '')})
        respuestas = {preg['label']: f.get(f'prueba_{i}', '')
                      for i, preg in enumerate(preguntas)}

        conn.execute("""
            INSERT INTO alumnos
              (curso_id,nombre,apellidos,fecha_nacimiento,domicilio,numero,puerta,
               poblacion,cp,telefono,telefono_emergencias,email,edad,sexo,nacionalidad,
               dni,seg_social,expediente,fecha_inscripcion_servef,oficina_servef,
               cobra_prestaciones,situacion_laboral,titulacion,titulacion_especialidad,
               estudia_actualmente,que_estudia,cursos_fpo,experiencia,desea_realizar,
               motivacion,tiene_carnet,tiene_vehiculo,razon_interes,razon_motivacion,
               razon_empleo,razon_impedimentos,razon_limitaciones,observaciones,
               acepta_compromiso,respuestas_prueba)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            cid, f.get('nombre', ''), f.get('apellidos', ''),
            f.get('fecha_nacimiento', ''), f.get('domicilio', ''), f.get('numero', ''),
            f.get('puerta', ''), f.get('poblacion', ''), f.get('cp', ''),
            f.get('telefono', ''), f.get('telefono_emergencias', ''), email,
            f.get('edad', ''), f.get('sexo', ''), f.get('nacionalidad', ''),
            f.get('dni', ''), f.get('seg_social', ''), f.get('expediente', ''),
            f.get('fecha_inscripcion_servef', ''), f.get('oficina_servef', ''),
            f.get('cobra_prestaciones', ''), f.get('situacion_laboral', ''),
            f.get('titulacion', ''), f.get('titulacion_especialidad', ''),
            f.get('estudia_actualmente', ''), f.get('que_estudia', ''),
            json.dumps(fpo, ensure_ascii=False), json.dumps(exp, ensure_ascii=False),
            f.get('desea_realizar', ''), f.get('motivacion', ''),
            f.get('tiene_carnet', ''), f.get('tiene_vehiculo', ''),
            f.get('razon_interes', ''), f.get('razon_motivacion', ''),
            f.get('razon_empleo', ''), f.get('razon_impedimentos', ''),
            f.get('razon_limitaciones', ''), f.get('observaciones', ''),
            f.get('acepta_compromiso', ''),
            json.dumps(respuestas, ensure_ascii=False)
        ))
        conn.commit()
        _notificar_admin_inscripcion(f.get('nombre', ''), f.get('apellidos', ''),
                                     curso['nombre'], conn)
        return render_template('inscripcion_ok.html', curso=curso)

    return render_template('inscripcion.html', curso=curso, preguntas=preguntas,
                           errors=[], form={})


# ── ALUMNOS ───────────────────────────────────────────────────────────────────

@app.route('/alumno/<int:aid>')
@login_required
def ver_alumno(aid):
    conn   = get_db()
    alumno = conn.execute("""
        SELECT a.*, c.nombre as curso_nombre, c.id as curso_id, c.plazas as curso_plazas
        FROM alumnos a JOIN cursos c ON c.id=a.curso_id WHERE a.id=?
    """, (aid,)).fetchone()
    if not alumno:
        return 'No encontrado', 404

    stats = conn.execute("""
        SELECT SUM(CASE WHEN estado='seleccionado' THEN 1 ELSE 0 END) as seleccionados
        FROM alumnos WHERE curso_id=?
    """, (alumno['curso_id'],)).fetchone()

    fpo        = json.loads(alumno['cursos_fpo'] or '[]')
    exp        = json.loads(alumno['experiencia'] or '[]')
    analisis   = json.loads(alumno['analisis_ia']) if alumno['analisis_ia'] else None
    respuestas = json.loads(alumno['respuestas_prueba'] or '{}')
    plazas_llenas = (stats['seleccionados'] or 0) >= (alumno['curso_plazas'] or 0)

    documentos = conn.execute(
        'SELECT * FROM documentos WHERE alumno_id=? ORDER BY creado_en', (aid,)).fetchall()
    historial  = conn.execute(
        'SELECT * FROM historial WHERE alumno_id=? ORDER BY creado_en DESC LIMIT 30',
        (aid,)).fetchall()

    return render_template('ver_alumno.html', alumno=alumno, fpo=fpo, exp=exp,
                           analisis=analisis, respuestas=respuestas,
                           plazas_llenas=plazas_llenas, documentos=documentos,
                           historial=historial)


@app.route('/alumno/<int:aid>/estado', methods=['POST'])
@login_required
@csrf.exempt
def cambiar_estado(aid):
    estado = request.json.get('estado', '')
    if estado not in ['pendiente', 'seleccionado', 'no_seleccionado', 'reserva']:
        return jsonify({'error': 'inválido'}), 400
    conn   = get_db()
    alumno = conn.execute("""
        SELECT a.*, c.nombre as curso_nombre, c.plazas as curso_plazas,
               c.id as cid
        FROM alumnos a JOIN cursos c ON c.id=a.curso_id WHERE a.id=?
    """, (aid,)).fetchone()
    if not alumno:
        return jsonify({'error': 'no encontrado'}), 404

    estado_anterior = alumno['estado']
    conn.execute('UPDATE alumnos SET estado=? WHERE id=?', (estado, aid))
    _log_historial(conn, aid, alumno['cid'], session.get('user_nombre', ''),
                   'Cambio de estado', f'{estado_anterior} → {estado}')
    conn.commit()

    aviso = None
    if estado == 'seleccionado':
        s = conn.execute("""
            SELECT SUM(CASE WHEN estado='seleccionado' THEN 1 ELSE 0 END) as sel
            FROM alumnos WHERE curso_id=?
        """, (alumno['cid'],)).fetchone()
        if s and (s['sel'] or 0) >= (alumno['curso_plazas'] or 0):
            aviso = 'plazas_llenas'

    # Enviar email de notificación si hay email y estado final
    if estado in ('seleccionado', 'reserva', 'no_seleccionado') and alumno['email']:
        pos = None
        if alumno['puntuacion_ia'] and alumno['analisis_ia']:
            try:
                pos = json.loads(alumno['analisis_ia']).get('posicion')
            except Exception:
                pass
        _enviar_email_estado(alumno['email'], alumno['nombre'],
                             alumno['curso_nombre'], estado, pos)

    return jsonify({'ok': True, 'estado': estado, 'aviso': aviso})


@app.route('/alumno/<int:aid>/expediente', methods=['POST'])
@login_required
def guardar_expediente(aid):
    expediente = request.form.get('expediente', '').strip()
    conn       = get_db()
    conn.execute('UPDATE alumnos SET expediente=? WHERE id=?', (expediente, aid))
    conn.commit()
    return redirect(url_for('ver_alumno', aid=aid))


@app.route('/alumno/<int:aid>/notas', methods=['POST'])
@login_required
def guardar_notas(aid):
    notas = request.form.get('notas', '').strip()
    conn  = get_db()
    conn.execute('UPDATE alumnos SET notas=? WHERE id=?', (notas, aid))
    conn.commit()
    return redirect(url_for('ver_alumno', aid=aid))


@app.route('/alumno/<int:aid>/editar', methods=['GET', 'POST'])
@login_required
def editar_alumno(aid):
    conn   = get_db()
    alumno = conn.execute("""
        SELECT a.*, c.nombre as curso_nombre, c.id as curso_id,
               c.preguntas_prueba
        FROM alumnos a JOIN cursos c ON c.id=a.curso_id WHERE a.id=?
    """, (aid,)).fetchone()
    if not alumno:
        return 'No encontrado', 404
    preguntas = json.loads(alumno['preguntas_prueba'] or '[]')

    if request.method == 'POST':
        f = request.form
        fpo, exp = [], []
        for i in range(1, 4):
            e = f.get(f'fpo_esp_{i}', '').strip()
            if e:
                fpo.append({'especialidad': e, 'horas': f.get(f'fpo_h_{i}', ''),
                            'anyo': f.get(f'fpo_a_{i}', '')})
        for i in range(1, 4):
            p = f.get(f'exp_p_{i}', '').strip()
            if p:
                exp.append({'profesion': p, 'categoria': f.get(f'exp_c_{i}', ''),
                            'anios': f.get(f'exp_a_{i}', ''), 'meses': f.get(f'exp_m_{i}', ''),
                            'empresa': f.get(f'exp_e_{i}', '')})
        respuestas = {preg['label']: f.get(f'prueba_{i}', '')
                      for i, preg in enumerate(preguntas)}
        conn.execute("""
            UPDATE alumnos SET
              nombre=?,apellidos=?,fecha_nacimiento=?,domicilio=?,numero=?,puerta=?,
              poblacion=?,cp=?,telefono=?,telefono_emergencias=?,email=?,edad=?,sexo=?,
              nacionalidad=?,dni=?,seg_social=?,fecha_inscripcion_servef=?,oficina_servef=?,
              cobra_prestaciones=?,situacion_laboral=?,titulacion=?,titulacion_especialidad=?,
              estudia_actualmente=?,que_estudia=?,cursos_fpo=?,experiencia=?,desea_realizar=?,
              motivacion=?,tiene_carnet=?,tiene_vehiculo=?,razon_interes=?,razon_motivacion=?,
              razon_empleo=?,razon_impedimentos=?,razon_limitaciones=?,observaciones=?,
              respuestas_prueba=?
            WHERE id=?
        """, (
            f.get('nombre',''), f.get('apellidos',''), f.get('fecha_nacimiento',''),
            f.get('domicilio',''), f.get('numero',''), f.get('puerta',''),
            f.get('poblacion',''), f.get('cp',''), f.get('telefono',''),
            f.get('telefono_emergencias',''), f.get('email',''),
            f.get('edad',''), f.get('sexo',''), f.get('nacionalidad',''),
            f.get('dni',''), f.get('seg_social',''),
            f.get('fecha_inscripcion_servef',''), f.get('oficina_servef',''),
            f.get('cobra_prestaciones',''), f.get('situacion_laboral',''),
            f.get('titulacion',''), f.get('titulacion_especialidad',''),
            f.get('estudia_actualmente',''), f.get('que_estudia',''),
            json.dumps(fpo, ensure_ascii=False), json.dumps(exp, ensure_ascii=False),
            f.get('desea_realizar',''), f.get('motivacion',''),
            f.get('tiene_carnet',''), f.get('tiene_vehiculo',''),
            f.get('razon_interes',''), f.get('razon_motivacion',''),
            f.get('razon_empleo',''), f.get('razon_impedimentos',''),
            f.get('razon_limitaciones',''), f.get('observaciones',''),
            json.dumps(respuestas, ensure_ascii=False),
            aid
        ))
        _log_historial(conn, aid, alumno['curso_id'], session.get('user_nombre', ''),
                       'Edición de datos', 'Datos del alumno actualizados')
        conn.commit()
        return redirect(url_for('ver_alumno', aid=aid))

    fpo = json.loads(alumno['cursos_fpo'] or '[]')
    exp = json.loads(alumno['experiencia'] or '[]')
    respuestas = json.loads(alumno['respuestas_prueba'] or '{}')
    return render_template('editar_alumno.html', alumno=alumno, fpo=fpo, exp=exp,
                           preguntas=preguntas, respuestas=respuestas)


@app.route('/alumno/<int:aid>/documentos', methods=['POST'])
@login_required
def subir_documento(aid):
    conn   = get_db()
    alumno = conn.execute('SELECT id FROM alumnos WHERE id=?', (aid,)).fetchone()
    if not alumno:
        abort(404)
    archivo = request.files.get('archivo')
    tipo    = request.form.get('tipo', 'otro')
    if not archivo or not archivo.filename:
        return redirect(url_for('ver_alumno', aid=aid))

    ext = Path(secure_filename(archivo.filename)).suffix.lower()
    if ext not in EXTENSIONES_OK:
        return redirect(url_for('ver_alumno', aid=aid))

    carpeta = UPLOADS_DIR / str(aid)
    carpeta.mkdir(exist_ok=True)
    nombre_fichero = f"{tipo}_{os.urandom(6).hex()}{ext}"
    archivo.save(carpeta / nombre_fichero)

    conn.execute(
        'INSERT INTO documentos (alumno_id,tipo,nombre_original,nombre_fichero) VALUES (?,?,?,?)',
        (aid, tipo, secure_filename(archivo.filename), nombre_fichero))
    conn.commit()
    return redirect(url_for('ver_alumno', aid=aid))


@app.route('/alumno/<int:aid>/documentos/<int:did>/descargar')
@login_required
def descargar_documento(aid, did):
    conn = get_db()
    doc  = conn.execute(
        'SELECT * FROM documentos WHERE id=? AND alumno_id=?', (did, aid)).fetchone()
    if not doc:
        abort(404)
    ruta = UPLOADS_DIR / str(aid) / doc['nombre_fichero']
    if not ruta.exists():
        abort(404)
    return send_file(ruta, download_name=doc['nombre_original'], as_attachment=False)


@app.route('/alumno/<int:aid>/documentos/<int:did>/eliminar', methods=['POST'])
@login_required
def eliminar_documento(aid, did):
    conn = get_db()
    doc  = conn.execute(
        'SELECT * FROM documentos WHERE id=? AND alumno_id=?', (did, aid)).fetchone()
    if doc:
        ruta = UPLOADS_DIR / str(aid) / doc['nombre_fichero']
        ruta.unlink(missing_ok=True)
        conn.execute('DELETE FROM documentos WHERE id=?', (did,))
        conn.commit()
    return redirect(url_for('ver_alumno', aid=aid))


@app.route('/alumno/<int:aid>/eliminar', methods=['POST'])
@login_required
def eliminar_alumno(aid):
    conn = get_db()
    a    = conn.execute('SELECT curso_id FROM alumnos WHERE id=?', (aid,)).fetchone()
    cid  = a['curso_id'] if a else 1
    # Eliminar documentos del alumno
    docs = conn.execute('SELECT nombre_fichero FROM documentos WHERE alumno_id=?', (aid,)).fetchall()
    for d in docs:
        (UPLOADS_DIR / str(aid) / d['nombre_fichero']).unlink(missing_ok=True)
    conn.execute('DELETE FROM documentos WHERE alumno_id=?', (aid,))
    conn.execute('DELETE FROM alumnos WHERE id=?', (aid,))
    conn.commit()
    return redirect(url_for('ver_curso', cid=cid))


# ── RANKING IA ────────────────────────────────────────────────────────────────

@app.route('/cursos/<int:cid>/ranking-ia', methods=['POST'])
@login_required
@csrf.exempt
def ranking_ia(cid):
    conn    = get_db()
    curso   = conn.execute('SELECT * FROM cursos WHERE id=?', (cid,)).fetchone()
    alumnos = conn.execute('SELECT * FROM alumnos WHERE curso_id=?', (cid,)).fetchall()
    if not alumnos:
        return jsonify({'error': 'Sin alumnos'}), 400

    candidatos = []
    for a in alumnos:
        fpo        = json.loads(a['cursos_fpo'] or '[]')
        exp        = json.loads(a['experiencia'] or '[]')
        respuestas = json.loads(a['respuestas_prueba'] or '{}')
        meses = sum(
            int(e.get('anios', 0) or 0) * 12 + int(e.get('meses', 0) or 0)
            for e in exp
        )
        candidatos.append({
            'id': a['id'], 'nombre': f"{a['nombre']} {a['apellidos']}",
            'situacion_laboral': a['situacion_laboral'],
            'cobra_prestaciones': a['cobra_prestaciones'],
            'titulacion': a['titulacion'], 'n_cursos_fpo': len(fpo),
            'meses_experiencia': meses,
            'motivacion': (a['motivacion'] or '')[:300],
            'tiene_carnet': a['tiene_carnet'],
            'razon_interes': (a['razon_interes'] or '')[:200],
            'razon_motivacion': (a['razon_motivacion'] or '')[:200],
            'razon_empleo': (a['razon_empleo'] or '')[:200],
            'respuestas_prueba_especifica': respuestas,
        })

    n       = len(candidatos)
    ids_esp = [c['id'] for c in candidatos]
    ejemplo = ','.join([
        f'{{"alumno_id":{c["id"]},"posicion":{i+1},"puntuacion":7.0,"resumen":"resumen",'
        f'"puntos_fuertes":["x"],"puntos_debiles":["x"]}}'
        for i, c in enumerate(candidatos)
    ])
    prompt = f"""Eres experto en seleccion FPO Labora (Comunitat Valenciana).
Curso: {curso['nombre']} | Plazas: {curso['plazas']}

IMPORTANTE: Debes analizar y puntuar a los {n} candidatos. El ranking DEBE tener exactamente {n} entradas.

Candidatos: {json.dumps(candidatos, ensure_ascii=False)}

Criterios (orden de importancia):
1. Situacion laboral: Sin empleo >12m > 6-12m > <6m > primera vez > otros > con contrato
2. Cobra prestaciones Si > No
3. Calidad de motivacion y respuestas a la prueba especifica
4. Formacion previa y cursos FPO anteriores
5. Meses de experiencia profesional

REGLAS JSON:
- Array ranking con exactamente {n} objetos
- IDs alumno_id exactamente: {ids_esp}
- Posicion 1 (mejor) a {n} (peor)
- Puntuacion 0.0 a 10.0

Responde SOLO con JSON, sin markdown:
{{"ranking":[{ejemplo}]}}"""

    try:
        payload = json.dumps({
            'model': OLLAMA_MODEL, 'prompt': prompt, 'stream': False,
            'options': {'temperature': 0.1, 'num_predict': 2000}
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=180) as r:
            result = json.loads(r.read())
        text  = result.get('response', '')
        s, e_ = text.find('{'), text.rfind('}') + 1
        if s < 0 or e_ <= s:
            raise ValueError('JSON no encontrado en respuesta de Ollama')
        data    = _parse_json_robusto(text[s:e_])
        ranking = data.get('ranking', [])

        ids_rec = {item['alumno_id'] for item in ranking}
        for c in candidatos:
            if c['id'] not in ids_rec:
                ranking.append({'alumno_id': c['id'], 'posicion': len(ranking) + 1,
                                'puntuacion': 5.0, 'resumen': 'Análisis no disponible.',
                                'puntos_fuertes': [], 'puntos_debiles': []})
        ranking.sort(key=lambda x: x['puntuacion'], reverse=True)
        for i, item in enumerate(ranking):
            item['posicion'] = i + 1

        conn = get_db()
        for item in ranking:
            analisis_json = json.dumps({
                'posicion': item['posicion'], 'resumen': item['resumen'],
                'puntos_fuertes': item.get('puntos_fuertes', []),
                'puntos_debiles': item.get('puntos_debiles', []),
            }, ensure_ascii=False)
            conn.execute('UPDATE alumnos SET puntuacion_ia=?,analisis_ia=? WHERE id=?',
                         (item['puntuacion'], analisis_json, item['alumno_id']))
        conn.commit()
        return jsonify({'ok': True, 'ranking': ranking})
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _log_historial(conn, alumno_id: int, curso_id: int,
                   usuario: str, accion: str, detalle: str):
    conn.execute(
        'INSERT INTO historial (alumno_id,curso_id,usuario_nombre,accion,detalle) VALUES (?,?,?,?,?)',
        (alumno_id, curso_id, usuario, accion, detalle))


def _notificar_admin_inscripcion(nombre: str, apellidos: str, curso: str, conn):
    admins = conn.execute('SELECT email FROM usuarios').fetchall()
    server   = os.environ.get('MAIL_SERVER', '')
    port     = int(os.environ.get('MAIL_PORT', 587))
    username = os.environ.get('MAIL_USERNAME', '')
    password = os.environ.get('MAIL_PASSWORD', '')
    from_    = os.environ.get('MAIL_FROM', username)
    if not server or not username or not password:
        return
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:1.5rem;
                border:1px solid #e8e8e4;border-radius:10px;">
      <p style="font-size:0.75rem;text-transform:uppercase;color:#b5a050;font-weight:600;">
        TAME Formación · Nueva inscripción</p>
      <h2 style="color:#3d5f6e;font-size:1.3rem;">Nueva solicitud recibida</h2>
      <p style="margin:0.8rem 0;">
        <strong>{nombre} {apellidos}</strong> se ha inscrito en el curso:<br>
        <em style="color:#5a7f8f;">{curso}</em>
      </p>
      <p style="font-size:0.85rem;color:#888;">Accede al panel para revisar su solicitud.</p>
    </div>"""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'[TAME] Nueva inscripción – {nombre} {apellidos}'
        msg['From']    = from_
        msg['To']      = ', '.join(r['email'] for r in admins)
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP(server, port, timeout=8) as s:
            s.starttls(); s.login(username, password)
            s.sendmail(username, [r['email'] for r in admins], msg.as_string())
    except Exception:
        pass


def _enviar_email_estado(to: str, nombre: str, curso: str, estado: str, posicion=None):
    """Envía notificación de estado al alumno. Silencia errores para no bloquear la app."""
    server   = os.environ.get('MAIL_SERVER', '')
    port     = int(os.environ.get('MAIL_PORT', 587))
    username = os.environ.get('MAIL_USERNAME', '')
    password = os.environ.get('MAIL_PASSWORD', '')
    from_    = os.environ.get('MAIL_FROM', username)

    if not server or not username or not password:
        return  # No hay config de correo, ignorar

    etiquetas = {
        'seleccionado':    ('¡Enhorabuena! Ha sido seleccionado/a', '#4a7c59'),
        'reserva':         ('Ha quedado en lista de reserva',        '#b07030'),
        'no_seleccionado': ('No ha sido seleccionado/a',             '#a04040'),
    }
    asunto_base, color = etiquetas.get(estado, ('Actualización de su solicitud', '#5a7f8f'))
    pos_txt = f'<p style="font-size:1.1rem;margin:1rem 0;">Su posición en el ranking: <strong>#{posicion}</strong></p>' if posicion else ''

    html = f"""
    <div style="font-family:'DM Sans',Arial,sans-serif;max-width:520px;margin:auto;padding:2rem;
                border:1px solid #e8e8e4;border-radius:12px;color:#1a1a18;">
      <p style="font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;color:#b5a050;font-weight:600;">
        TAME Formación · FPO Labora
      </p>
      <h2 style="font-size:1.5rem;color:#3d5f6e;border-bottom:2px solid {color};
                 padding-bottom:0.5rem;margin-bottom:1rem;">{asunto_base}</h2>
      <p>Estimado/a <strong>{nombre}</strong>,</p>
      <p style="margin:0.8rem 0;">Le informamos del resultado de su solicitud para el curso:</p>
      <p style="font-style:italic;color:#5a7f8f;font-size:1.05rem;margin:0.5rem 0 1rem;">{curso}</p>
      {pos_txt}
      <p>El equipo de TAME Formación se pondrá en contacto con usted si necesita más información.</p>
      <p style="margin-top:2rem;font-size:0.78rem;color:#888880;">
        © TAME Formación · Mislata Formación S.L.
      </p>
    </div>
    """
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'[TAME Formación] {asunto_base} – {curso}'
        msg['From']    = from_
        msg['To']      = to
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP(server, port, timeout=10) as s:
            s.starttls()
            s.login(username, password)
            s.sendmail(username, to, msg.as_string())
    except Exception:
        pass  # No interrumpir la app si el email falla


def _parse_json_robusto(text: str) -> dict:
    """Intenta parsear el JSON de Ollama; si falla limpia caracteres problemáticos."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Eliminar saltos de línea y tabulaciones dentro de strings JSON
    import re as _re
    # Reemplazar newlines dentro de valores de cadena por \n escapado
    cleaned = _re.sub(r'(?<=:)\s*"(.*?)"(?=\s*[,}\]])',
                      lambda m: '"' + m.group(1).replace('\n', ' ').replace('\r', '').replace('"', '\\"') + '"',
                      text, flags=_re.DOTALL)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: extraer items individuales con regex
    items = []
    for m in _re.finditer(
        r'\{[^{}]*"alumno_id"\s*:\s*(\d+)[^{}]*"puntuacion"\s*:\s*([\d.]+)[^{}]*\}',
        text, _re.DOTALL
    ):
        try:
            items.append(json.loads(m.group(0)))
        except Exception:
            raw = m.group(0)
            aid = int(_re.search(r'"alumno_id"\s*:\s*(\d+)', raw).group(1))
            pun = float(_re.search(r'"puntuacion"\s*:\s*([\d.]+)', raw).group(1))
            items.append({'alumno_id': aid, 'puntuacion': pun,
                          'resumen': 'Análisis generado (texto simplificado).',
                          'puntos_fuertes': [], 'puntos_debiles': []})
    if items:
        return {'ranking': items}

    raise ValueError('No se pudo parsear la respuesta de Ollama como JSON válido')


def _generar_qr(url: str) -> str:
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#1a1a18', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()


if __name__ == '__main__':
    init_db()
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, port=5000)
