from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from datetime import datetime
import os
import cloudinary
import cloudinary.uploader
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_cors import CORS

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-key-for-local-dev')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
database_url = os.environ.get('DATABASE_URL', 'sqlite:///recipes.db')
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'jwt-secret-key')
CORS(app)
jwt = JWTManager(app)

# Render gives a URL starting with postgres:// but SQLAlchemy needs postgresql://
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url

cloudinary.config(
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key    = os.environ.get('CLOUDINARY_API_KEY'),
    api_secret = os.environ.get('CLOUDINARY_API_SECRET')
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

favorites = db.Table('favorites',
    db.Column('user_id',   db.Integer, db.ForeignKey('user.id'),   primary_key=True),
    db.Column('recipe_id', db.Integer, db.ForeignKey('recipe.id'), primary_key=True)
)

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ─── Model ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    recipes       = db.relationship('Recipe', backref='author', lazy=True)
    favorited_recipes = db.relationship('Recipe', secondary=favorites, lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_active(self): return True
    def is_authenticated(self): return True
    def is_anonymous(self): return False
    def get_id(self): return str(self.id)


class Recipe(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    title         = db.Column(db.String(200), nullable=False)
    description   = db.Column(db.Text)
    ingredients   = db.Column(db.Text, nullable=False)   # newline-separated
    instructions  = db.Column(db.Text, nullable=False)   # newline-separated steps
    category      = db.Column(db.String(50), default='Other')
    prep_time     = db.Column(db.Integer)                # minutes
    cook_time     = db.Column(db.Integer)                # minutes
    servings      = db.Column(db.Integer)
    image_url     = db.Column(db.String(500))
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    @property
    def ingredients_list(self):
        return [i.strip() for i in self.ingredients.split('\n') if i.strip()]

    @property
    def instructions_list(self):
        return [s.strip() for s in self.instructions.split('\n') if s.strip()]


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # Read all filter params from the URL
    query       = request.args.get('q', '').strip()
    category    = request.args.get('category', '')
    ingredient  = request.args.get('ingredient', '').strip()
    max_time    = request.args.get('max_time', '')
    max_serving = request.args.get('max_servings', '')

    # THIS LINE MUST COME BEFORE ANY FILTERS
    recipes_query = Recipe.query

    # Filter by title
    if query:
        recipes_query = recipes_query.filter(Recipe.title.ilike(f'%{query}%'))

    # Filter by category
    if category:
        recipes_query = recipes_query.filter_by(category=category)

    # Filter by multiple ingredients (comma-separated, must match ALL)
    ingredient_terms = [t.strip() for t in ingredient.split(',') if t.strip()]
    for term in ingredient_terms:
        recipes_query = recipes_query.filter(
            Recipe.ingredients.ilike(f'%{term}%')
        )

    # Filter by total cooking time (prep + cook), treating NULL as 0
    if max_time:
        total_time = (
            func.coalesce(Recipe.prep_time, 0) +
            func.coalesce(Recipe.cook_time, 0)
        )
        recipes_query = recipes_query.filter(total_time <= int(max_time))

    # Filter by max servings
    if max_serving:
        recipes_query = recipes_query.filter(
            Recipe.servings <= int(max_serving)
        )

    recipes    = recipes_query.order_by(Recipe.created_at.desc()).all()
    categories = [c[0] for c in db.session.query(Recipe.category).distinct().all()]

    any_filter = any([query, category, ingredient, max_time, max_serving])

    return render_template('index.html',
                           recipes=recipes,
                           categories=categories,
                           query=query,
                           selected_category=category,
                           ingredient=ingredient,
                           max_time=max_time,
                           max_servings=max_serving,
                           any_filter=any_filter)


@app.route('/recipe/<int:recipe_id>')
def recipe_detail(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    return render_template('recipe_detail.html', recipe=recipe)


@app.route('/recipe/new', methods=['GET', 'POST'])
@login_required
def new_recipe():
    if request.method == 'POST':
        image_url = ''
        file = request.files.get('image')
        if file and file.filename and allowed_file(file.filename):
            result    = cloudinary.uploader.upload(file)
            image_url = result['secure_url']

        recipe = Recipe(
            title        = request.form.get('title', '').strip(),
            description  = request.form.get('description', '').strip(),
            ingredients  = request.form.get('ingredients', '').strip(),
            instructions = request.form.get('instructions', '').strip(),
            category     = request.form.get('category', 'Other'),
            prep_time    = int(request.form.get('prep_time') or 0),
            cook_time    = int(request.form.get('cook_time') or 0),
            servings     = int(request.form.get('servings') or 1),
            image_url    = image_url,
            user_id      = current_user.id,
        )
        db.session.add(recipe)
        db.session.commit()
        flash('Recipe created!', 'success')
        return redirect(url_for('recipe_detail', recipe_id=recipe.id))

    return render_template('recipe_form.html', recipe=None)


@app.route('/recipe/<int:recipe_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    if recipe.user_id != current_user.id:
        flash('You can only edit your own recipes.', 'error')
        return redirect(url_for('recipe_detail', recipe_id=recipe.id))

    if request.method == 'POST':
        file = request.files.get('image')
        if file and file.filename and allowed_file(file.filename):
            result           = cloudinary.uploader.upload(file)
            recipe.image_url = result['secure_url']

        recipe.title        = request.form.get('title', '').strip()
        recipe.description  = request.form.get('description', '').strip()
        recipe.ingredients  = request.form.get('ingredients', '').strip()
        recipe.instructions = request.form.get('instructions', '').strip()
        recipe.category     = request.form.get('category', 'Other')
        recipe.prep_time    = int(request.form.get('prep_time') or 0)
        recipe.cook_time    = int(request.form.get('cook_time') or 0)
        recipe.servings     = int(request.form.get('servings') or 1)
        recipe.updated_at   = datetime.utcnow()
        db.session.commit()
        flash('Recipe updated!', 'success')
        return redirect(url_for('recipe_detail', recipe_id=recipe.id))

    return render_template('recipe_form.html', recipe=recipe)


@app.route('/recipe/<int:recipe_id>/delete', methods=['POST'])
@login_required
def delete_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    if recipe.user_id != current_user.id:
        flash('You can only delete your own recipes.', 'error')
        return redirect(url_for('recipe_detail', recipe_id=recipe.id))
    db.session.delete(recipe)
    db.session.commit()
    flash('Recipe deleted.', 'info')
    return redirect(url_for('index'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'error')
        elif User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('Account created! Welcome!', 'success')
            return redirect(url_for('index'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(request.args.get('next') or url_for('index'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/recipe/<int:recipe_id>/favorite', methods=['POST'])
@login_required
def toggle_favorite(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    if recipe in current_user.favorited_recipes:
        current_user.favorited_recipes.remove(recipe)
        flash('Removed from favorites.', 'info')
    else:
        current_user.favorited_recipes.append(recipe)
        flash('Added to favorites!', 'success')
    db.session.commit()
    return redirect(url_for('recipe_detail', recipe_id=recipe.id))


@app.route('/favorites')
@login_required
def favorites_page():
    recipes = current_user.favorited_recipes.all()
    return render_template('favorites.html', recipes=recipes)

@app.route('/user/<username>')
def user_profile(username):
    user    = User.query.filter_by(username=username).first_or_404()
    recipes = Recipe.query.filter_by(user_id=user.id).order_by(Recipe.created_at.desc()).all()
    return render_template('profile.html', profile_user=user, recipes=recipes)


# ─── API (bonus) ──────────────────────────────────────────────────────────────

@app.route('/api/recipes')
def api_recipes():
    recipes = Recipe.query.order_by(Recipe.created_at.desc()).all()
    return jsonify([{
        'id':        r.id,
        'title':     r.title,
        'category':  r.category,
        'prep_time': r.prep_time,
        'cook_time': r.cook_time,
        'servings':  r.servings,
        'image_url': r.image_url,
    } for r in recipes])


# ─── Mobile API Routes ────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def api_register():
    try:
        data     = request.get_json(force=True)
        username = data.get('username', '').strip()
        email    = data.get('email', '').strip()
        password = data.get('password', '')

        if not username or not email or not password:
            return jsonify({'error': 'All fields are required'}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username already taken'}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already registered'}), 400

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        token = create_access_token(identity=str(user.id))
        return jsonify({'token': token, 'username': user.username}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data     = request.get_json(force=True)
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({'error': 'Username and password are required'}), 400

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            return jsonify({'error': 'Invalid username or password'}), 401

        token = create_access_token(identity=str(user.id))
        return jsonify({'token': token, 'username': user.username}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recipes', methods=['GET'])
def api_get_recipes():
    query      = request.args.get('q', '').strip()
    category   = request.args.get('category', '')
    ingredient = request.args.get('ingredient', '').strip()
    max_time   = request.args.get('max_time', '')

    recipes_query = Recipe.query

    if query:
        recipes_query = recipes_query.filter(Recipe.title.ilike(f'%{query}%'))
    if category:
        recipes_query = recipes_query.filter_by(category=category)

    ingredient_terms = [t.strip() for t in ingredient.split(',') if t.strip()]
    for term in ingredient_terms:
        recipes_query = recipes_query.filter(Recipe.ingredients.ilike(f'%{term}%'))

    if max_time:
        total_time = func.coalesce(Recipe.prep_time, 0) + func.coalesce(Recipe.cook_time, 0)
        recipes_query = recipes_query.filter(total_time <= int(max_time))

    recipes = recipes_query.order_by(Recipe.created_at.desc()).all()
    return jsonify([{
        'id':           r.id,
        'title':        r.title,
        'description':  r.description,
        'category':     r.category,
        'ingredients':  r.ingredients,
        'instructions': r.instructions,
        'prep_time':    r.prep_time,
        'cook_time':    r.cook_time,
        'servings':     r.servings,
        'image_url':    r.image_url,
        'notes':        r.notes,
        'author':       r.author.username,
        'user_id':      r.user_id,
    } for r in recipes])


@app.route('/api/recipes/<int:recipe_id>', methods=['GET'])
def api_get_recipe(recipe_id):
    try:
        r = Recipe.query.get_or_404(recipe_id)
        return jsonify({
            'id':           r.id,
            'title':        r.title or '',
            'description':  r.description or '',
            'category':     r.category or '',
            'ingredients':  r.ingredients or '',
            'instructions': r.instructions or '',
            'prep_time':    r.prep_time or 0,
            'cook_time':    r.cook_time or 0,
            'servings':     r.servings or 0,
            'image_url':    r.image_url or '',
            'notes':        r.notes or '',
            'author':       r.author.username if r.user_id and r.author else 'Unknown',
            'user_id':      r.user_id or 0,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/recipes', methods=['POST'])
@jwt_required()
def api_create_recipe():
    user_id = int(get_jwt_identity())
    data    = request.get_json()
    recipe  = Recipe(
        title        = data.get('title', '').strip(),
        description  = data.get('description', '').strip(),
        ingredients  = data.get('ingredients', '').strip(),
        instructions = data.get('instructions', '').strip(),
        category     = data.get('category', 'Other'),
        prep_time    = int(data.get('prep_time') or 0),
        cook_time    = int(data.get('cook_time') or 0),
        servings     = int(data.get('servings') or 1),
        image_url    = data.get('image_url', ''),
        notes        = data.get('notes', '').strip(),
        user_id      = user_id,
    )
    db.session.add(recipe)
    db.session.commit()
    return jsonify({'id': recipe.id, 'message': 'Recipe created'}), 201


@app.route('/api/recipes/<int:recipe_id>', methods=['DELETE'])
@jwt_required()
def api_delete_recipe(recipe_id):
    user_id = int(get_jwt_identity())
    recipe  = Recipe.query.get_or_404(recipe_id)
    if recipe.user_id != user_id:
        return jsonify({'error': 'Unauthorized'}), 403
    db.session.delete(recipe)
    db.session.commit()
    return jsonify({'message': 'Recipe deleted'}), 200


@app.route('/api/favorites', methods=['GET'])
@jwt_required()
def api_get_favorites():
    user_id = int(get_jwt_identity())
    user    = User.query.get_or_404(user_id)
    recipes = user.favorited_recipes.all()
    return jsonify([{
        'id':        r.id,
        'title':     r.title,
        'category':  r.category,
        'image_url': r.image_url,
        'prep_time': r.prep_time,
        'cook_time': r.cook_time,
        'servings':  r.servings,
    } for r in recipes])


@app.route('/api/favorites/<int:recipe_id>', methods=['POST'])
@jwt_required()
def api_toggle_favorite(recipe_id):
    user_id = int(get_jwt_identity())
    user    = User.query.get_or_404(user_id)
    recipe  = Recipe.query.get_or_404(recipe_id)

    if recipe in user.favorited_recipes:
        user.favorited_recipes.remove(recipe)
        db.session.commit()
        return jsonify({'favorited': False}), 200
    else:
        user.favorited_recipes.append(recipe)
        db.session.commit()
        return jsonify({'favorited': True}), 200


@app.route('/api/user/<username>', methods=['GET'])
def api_user_profile(username):
    user    = User.query.filter_by(username=username).first_or_404()
    recipes = Recipe.query.filter_by(user_id=user.id).order_by(Recipe.created_at.desc()).all()
    return jsonify({
        'username':    user.username,
        'member_since': user.created_at.strftime('%B %Y'),
        'recipe_count': len(recipes),
        'recipes': [{
            'id':        r.id,
            'title':     r.title,
            'category':  r.category,
            'image_url': r.image_url,
            'prep_time': r.prep_time,
            'cook_time': r.cook_time,
        } for r in recipes]
    })


with app.app_context():
    db.create_all()

# ─── Init ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)