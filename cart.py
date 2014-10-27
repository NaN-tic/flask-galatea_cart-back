from flask import Blueprint, render_template, current_app, g, url_for, \
    flash, redirect, session, request, jsonify
from galatea.tryton import tryton
from galatea.utils import thumbnail
from galatea.helpers import login_required
from flask.ext.babel import gettext as _, lazy_gettext as __
from flask.ext.wtf import Form
from wtforms import TextField, SelectField, IntegerField, validators
from decimal import Decimal
from emailvalid import check_email
import vatnumber

cart = Blueprint('cart', __name__, template_folder='templates')

SHOP = current_app.config.get('TRYTON_SALE_SHOP')
SHOPS = current_app.config.get('TRYTON_SALE_SHOPS')
CART_CROSSSELLS = current_app.config.get('TRYTON_CART_CROSSSELLS', True)
LIMIT_CROSSELLS = current_app.config.get('TRYTON_CATALOG_LIMIT_CROSSSELLS', 10)
MINI_CART_CODE = current_app.config.get('TRYTON_CATALOG_MINI_CART_CODE', False)

Cart = tryton.pool.get('sale.cart')
Template = tryton.pool.get('product.template')
Product = tryton.pool.get('product.product')
Shop = tryton.pool.get('sale.shop')
Carrier = tryton.pool.get('carrier')
Party = tryton.pool.get('party.party')
Address = tryton.pool.get('party.address')
Sale = tryton.pool.get('sale.sale')
SaleLine = tryton.pool.get('sale.line')

CART_FIELD_NAMES = [
    'cart_date', 'product_id', 'template_id', 'quantity', 'product.code',
    'product.rec_name', 'product.template.esale_slug', 'product.template.esale_default_images',
    'unit_price', 'unit_price_w_tax', 'untaxed_amount', 'amount_w_tax',
    ]
CART_ORDER = [
    ('cart_date', 'DESC'),
    ('id', 'DESC'),
    ]
from catalog.catalog import CATALOG_TEMPLATE_FIELD_NAMES

VAT_COUNTRIES = [('', '')]
for country in vatnumber.countries():
    VAT_COUNTRIES.append((country, country))

class ShipmentAddressForm(Form):
    "Shipment Address form"
    shipment_name = TextField(__('Name'), [validators.Required()])
    shipment_street = TextField(__('Street'), [validators.Required()])
    shipment_city = TextField(__('City'), [validators.Required()])
    shipment_zip = TextField(__('Zip'), [validators.Required()])
    shipment_country = SelectField(__('Country'), [validators.Required(), ], coerce=int)
    shipment_subdivision = IntegerField(__('Subdivision'), [validators.Required()])
    shipment_email = TextField(__('Email'), [validators.Required(), validators.Email()])
    shipment_phone = TextField(__('Phone'))
    vat_country = SelectField(__('VAT Country'), [validators.Required(), ])
    vat_number = TextField(__('VAT Number'), [validators.Required()])

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True

@cart.route('/json/my-cart', methods=['GET', 'PUT'], endpoint="my-cart")
@tryton.transaction()
def my_cart(lang):
    '''All Carts JSON'''
    items = []

    shop = Shop(SHOP)
    domain = [
        ('state', '=', 'draft'),
        ('shop', '=', SHOP),
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )

    carts = Cart.search_read(domain, order=CART_ORDER, fields_names=CART_FIELD_NAMES)

    decimals = "%0."+str(shop.esale_currency.digits)+"f" # "%0.2f" euro
    for cart in carts:
        img = cart['product.template.esale_default_images']
        image = current_app.config.get('BASE_IMAGE')
        if img.get('small'):
            thumbname = img['small']['name']
            filename = img['small']['digest']
            image = thumbnail(filename, thumbname, '200x200')
        items.append({
            'id': cart['id'],
            'name': cart['product.code'] if MINI_CART_CODE else cart['product.rec_name'],
            'url': url_for('catalog.product_'+g.language, lang=g.language,
                slug=cart['product.template.esale_slug']),
            'quantity': cart['quantity'],
            'unit_price': float(Decimal(decimals % cart['unit_price'])),
            'unit_price_w_tax': float(Decimal(decimals % cart['unit_price_w_tax'])),
            'untaxed_amount': float(Decimal(decimals % cart['untaxed_amount'])),
            'amount_w_tax': float(Decimal(decimals % cart['amount_w_tax'])),
            'image': image,
            })

    return jsonify(result={
        'currency': shop.esale_currency.symbol,
        'items': items,
        })

@cart.route("/confirm/", methods=["POST"], endpoint="confirm")
@tryton.transaction()
def confirm(lang):
    '''Convert carts to sale order
    Return to Sale Details
    '''
    shop = Shop(SHOP)
    data = request.form

    party = session.get('customer')
    shipment_address = data.get('shipment_address')
    name = data.get('shipment_name')
    email = data.get('shipment_email')

    # Get all carts
    domain = [
        ('state', '=', 'draft'),
        ('shop', '=', SHOP),
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search(domain)
    if not carts:
        flash(_('There are not products in your cart.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    # New party
    if party:
        party = Party(party)
    else:
        if not check_email(email):
            flash(_('Email "{email}" is not valid.').format(
                email=email), 'danger')
            return redirect(url_for('.cart', lang=g.language))

        party = Party.esale_create_party(shop, {
            'name': name,
            'esale_email': email,
            'vat_country': data.get('vat_country', None),
            'vat_number': data.get('vat_number', None),
            })
        session['customer'] = party.id

    if shipment_address != 'new-address':
        address = Address(shipment_address)
    else:
        country = None
        if data.get('shipment_country'):
            country = int(data.get('shipment_country'))
        subdivision = None
        if data.get('shipment_subdivision'):
            subdivision = int(data.get('shipment_subdivision'))

        values = {
            'name': name,
            'street': data.get('shipment_street'),
            'city': data.get('shipment_city'),
            'zip': data.get('shipment_zip'),
            'country': country,
            'subdivision': subdivision,
            'phone': data.get('shipment_phone'),
            'email': email,
            'fax': None,
            }
        address = Address.esale_create_address(shop, party, values)

    # Carts are same party to create a new sale
    Cart.write(carts, {'party': party})

    # Create new sale
    values = {}
    values['shipment_cost_method'] = 'order' # force shipment invoice on order
    values['shipment_address'] = address
    payment_type = data.get('payment_type')
    if payment_type:
        values['payment_type'] = int(payment_type)
    carrier = data.get('carrier')
    if carrier:
        values['carrier'] = int(carrier)
    comment = data.get('comment')
    if comment:
        values['comment'] = comment

    sales, error = Cart.create_sale(carts, values)
    if error:
        current_app.logger.error('Sale. Error create sale from party (%s): %s' % (party.id, error))
    if not sales:
        flash(_('It has not been able to convert the cart into an order. ' \
            'Try again or contact us.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))
    sale, = sales

    # Add shipment line
    carrier_price = data.get('carrier-cost')
    if carrier_price:
        product = shop.esale_delivery_product
        shipment_price = Decimal(carrier_price)
        shipment_line = SaleLine.get_shipment_line(product, shipment_price, sale)
        shipment_line.save()

    # sale draft to quotation
    Sale.quote([sale])

    if current_app.debug:
        current_app.logger.info('Sale. Create sale %s' % sale.id)

    flash(_('Sale order created successfully.'), 'success')

    return redirect(url_for('sale.sale', lang=g.language, id=sale.id))

@cart.route("/add/", methods=["POST"], endpoint="add")
@tryton.transaction()
def add(lang):
    '''Add product item cart'''
    to_create = []
    to_update = []
    to_remove = []
    to_remove_products = [] # Products in older cart and don't sell

    # Convert form values to dict values {'id': 'qty'}
    values = {}
    codes = []

    # json request
    if request.json:
        for data in request.json:
            if data.get('name'):
                product = data.get('name').split('-')
                try:
                    qty = float(data.get('value'))
                except:
                    qty = 1
                try:
                    values[int(product[1])] = qty
                except:
                    values[product[1]] = qty
                    codes.append(product[1])

        if not values:
            return jsonify(result=False)
    # post request
    else:
        for k, v in request.form.iteritems():
            product = k.split('-')
            if product[0] == 'product':
                try:
                    qty = float(v)
                except:
                    flash(_('You try to add no numeric quantity. ' \
                        'The request has been stopped.'))
                    return redirect(url_for('.cart', lang=g.language))
                try:
                    values[int(product[1])] = qty
                except:
                    values[product[1]] = qty
                    codes.append(product[1])

    # transform product code to id
    if codes:
        products = Product.search_read(
            [('code', 'in', codes)], fields_names=['code'])
        # reset dict
        vals = values.copy()
        values = {}

        for k, v in vals.items():
            for product in products:
                if product['code'] == k:
                    values[product['id']] = v
                    break

    # Remove items in cart
    removes = request.form.getlist('remove')

    # Products Current User Cart (products to send)
    products_current_cart = [k for k,v in values.iteritems()]

    # Search current cart by user or session
    domain = [
        ('state', '=', 'draft'),
        ('shop', '=', SHOP),
        ('product.id', 'in', products_current_cart)
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search(domain, order=[('cart_date', 'ASC')])

    # Products Current Cart (products available in sale.cart)
    products_in_cart = [c.product.id for c in carts]

    # Get product data
    products = Product.search([
        ('id', 'in', products_current_cart),
        ('template.esale_available', '=', True),
        ('template.esale_active', '=', True),
        ('template.esale_saleshops', 'in', SHOPS),
        ])

    # Delete products data
    if removes:
        for remove in removes:
            for cart in carts:
                try:
                    if cart.id == int(remove):
                        to_remove.append(cart)
                        break
                except:
                    flash(_('You try to remove no numeric cart. ' \
                        'The request has been stopped.'))
                    return redirect(url_for('.cart', lang=g.language))

    # Add/Update products data
    for product_id, qty in values.iteritems():
        for p in products:
            if p.id == product_id:
                product = p
                break

        if not product.add_cart:
            continue

        cart = Cart()
        cart.party = session.get('customer', None)
        cart.quantity = qty
        cart.product = product.id
        cart.sid = session.sid
        cart.galatea_user = session.get('user', None)
        vals = cart.on_change_product()

        # Create data
        if product_id not in products_in_cart and qty > 0:
            vals['party'] = session.get('customer', None)
            vals['quantity'] = qty
            vals['product'] = product.id
            vals['sid'] = session.sid
            vals['galatea_user'] = session.get('user', None)
            to_create.append(vals)

        # Update data
        if product_id in products_in_cart: 
            for cart in carts:
                if cart.product.id == product_id:
                    if qty > 0:
                        vals['quantity'] = qty
                        to_update.append({
                            'cart': cart,
                            'values': vals,
                            })
                    else: # Remove data when qty <= 0
                        to_remove.append(cart)
                    break

    # Add to remove older products
    if to_remove_products:
        for remove in to_remove_products:
            for cart in carts:
                if cart.product.id == remove:
                    to_remove.append(cart)
                    break

    # Add Cart
    if to_create:
        Cart.create(to_create)
        flash(_('{total} product/s have been added in your cart.').format(
            total=len(to_create)), 'success')

    # Update Cart
    if to_update:
        for update in to_update:
            Cart.write([update['cart']], update['values'])
        total = len(to_update)
        if to_remove:
            total = total-len(to_remove)
        flash(_('{total} product/s have been updated in your cart.').format(
            total=total), 'success')

    # Delete Cart
    if to_remove:
        Cart.delete(to_remove)
        flash(_('{total} product/s have been deleted in your cart.').format(
            total=len(to_remove)), 'success')

    if request.json:
        session.pop('_flashes', None)
        return jsonify(result=True)
    else:
        return redirect(url_for('.cart', lang=g.language))

@cart.route("/checkout/", methods=["GET", "POST"], endpoint="checkout")
@tryton.transaction()
def checkout(lang):
    '''Checkout user or session'''
    values = {}
    errors = []
    shop = Shop(SHOP)

    domain = [
        ('state', '=', 'draft'),
        ('shop', '=', SHOP),
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search_read(domain, order=CART_ORDER, fields_names=CART_FIELD_NAMES)
    if not carts:
        flash(_('There are not products in your cart.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    # Shipment Address
    #~ form_shipment_address = ShipmentAddressForm()
    shipment_address = request.form.get('shipment_address')
    if not shipment_address:
        flash(_('Select a Shipment Address.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))
    values['shipment_address'] = shipment_address
    if shipment_address == 'new-address':
        values['shipment_name'] = request.form.get('shipment_name')
        values['shipment_street'] = request.form.get('shipment_street')
        values['shipment_zip'] = request.form.get('shipment_zip')
        values['shipment_city'] = request.form.get('shipment_city')
        values['shipment_country'] = request.form.get('shipment_country')
        values['shipment_subdivision'] = request.form.get('shipment_subdivision')
        values['shipment_email'] = request.form.get('shipment_email')
        values['shipment_phone'] = request.form.get('shipment_phone')

        if not values['shipment_name'] or not values['shipment_street'] \
                or not values['shipment_zip'] or not values['shipment_city'] \
                or not values['shipment_email']:
            errors.append(_('Error when validate Shipment Address. ' \
                'Please, return to cart and complete Shipment Address'))

        if not check_email(values['shipment_email']):
            errors.append(_('Email not valid.'))

        vat_country = request.form.get('vat_country')
        vat_number = request.form.get('vat_number')
        values['vat_country'] = vat_country
        values['vat_number'] = vat_number

        vat_number = '%s%s' % (vat_country.upper(), vat_number)
        if not vatnumber.check_vat(vat_number):
            errors.append(_('VAT not valid.'))

    elif session.get('customer'):
        addresses = Address.search([
            ('party', '=', session['customer']),
            ('id', '=', int(shipment_address)),
            ('active', '=', True),
            ], order=[('sequence', 'ASC'), ('id', 'ASC')])
        if addresses:
            address, = addresses
            values['shipment_address_name'] = address.rec_name
        else:
            errors.append(_('We can found address related yours address. ' \
                'Please, select a new address in Shipment Address'))
    else:
        errors.append(_('You not select new address and not a customer. ' \
            'Please, select a new address in Shipment Address'))

    # Payment
    payment = int(request.form.get('payment'))
    for p in shop.esale_payments:
        if p.id == payment:
            values['payment'] = p.payment_type.id
            values['payment_name'] = p.payment_type.rec_name

    # Carrier
    carrier_id = request.form.get('carrier')
    if carrier_id:
        carrier = Carrier(carrier_id)
        carrier_price = carrier.get_sale_price() # return price, currency
        price = carrier_price[0]
        price_w_tax = carrier.get_sale_price_w_tax(price)
        values['carrier'] = carrier
        values['carrier_name'] = carrier.rec_name
        values['carrier_cost'] = price
        values['carrier_cost_w_tax'] = price_w_tax

    # Comment
    values['comment'] = request.form.get('comment')

    # Breadcumbs
    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }]

    # Breadcumbs Cart
    bcarts = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'slug': url_for('.checkout', lang=g.language),
        'name': _('Checkout'),
        }, {
        'name': _('Order'),
        }]

    return render_template('checkout.html',
            breadcrumbs=breadcrumbs,
            bcarts=bcarts,
            shop=shop,
            carts=carts,
            values=values,
            errors=errors,
            )

@cart.route("/", endpoint="cart")
@tryton.transaction()
def cart_list(lang):
    '''Cart by user or session'''
    shop = Shop(SHOP)

    form_shipment_address = ShipmentAddressForm(
        shipment_country=shop.esale_country.id,
        vat_country=shop.esale_country.code)
    countries = [(c.id, c.name) for c in shop.esale_countrys]
    form_shipment_address.shipment_country.choices = countries
    form_shipment_address.vat_country.choices = VAT_COUNTRIES

    domain = [
        ('state', '=', 'draft'),
        ('shop', '=', SHOP),
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search_read(domain, order=CART_ORDER, fields_names=CART_FIELD_NAMES)
    products = [cart['product_id'] for cart in carts]

    addresses = None
    if session.get('customer'):
        addresses = Address.search([
            ('party', '=', session['customer']),
            ('active', '=', True),
            ], order=[('sequence', 'ASC'), ('id', 'ASC')])

    stockable = Carrier.get_products_stockable(products)

    carriers = []
    if stockable:
        if session.get('customer'):
            party = Party(session.get('customer'))
            if hasattr(party, 'carrier'):
                carrier = party.carrier
                if carrier:
                    carrier_price = carrier.get_sale_price() # return price, currency
                    price = carrier_price[0]
                    price_w_tax = carrier.get_sale_price_w_tax(price)
                    carriers.append({
                        'id': party.carrier.id,
                        'name': party.carrier.rec_name,
                        'price': price,
                        'price_w_tax': price_w_tax,
                        })
        if not carriers:
            for c in shop.esale_carriers:
                carrier = c.carrier
                carrier_price = carrier.get_sale_price() # return price, currency
                price = carrier_price[0]
                price_w_tax = carrier.get_sale_price_w_tax(price)
                carriers.append({
                    'id': carrier.id,
                    'name': carrier.rec_name,
                    'price': price,
                    'price_w_tax': price_w_tax,
                    })

    # Cross Sells
    crossells = []
    if CART_CROSSSELLS:
        template_ids = []
        for cproduct in carts:
            template_ids.append(cproduct['template_id'])
        CATALOG_TEMPLATE_FIELD_NAMES.append('esale_crosssells')
        templates = Template.read(template_ids, CATALOG_TEMPLATE_FIELD_NAMES)
        crossells_ids = []
        for template in templates:
            for crossell in template['esale_crosssells']:
                if not crossell in crossells_ids and len(crossells_ids) < LIMIT_CROSSELLS:
                    crossells_ids.append(crossell)
        if crossells_ids:
            crossells = Template.read(crossells_ids, CATALOG_TEMPLATE_FIELD_NAMES)

    # Breadcumbs
    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }]

    # Breadcumbs Cart
    bcarts = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'name': _('Checkout'),
        }, {
        'name': _('Order'),
        }]

    return render_template('cart.html',
            breadcrumbs=breadcrumbs,
            bcarts=bcarts,
            shop=shop,
            carts=carts,
            form_shipment_address=form_shipment_address,
            addresses=addresses,
            crossells=crossells,
            carriers=sorted(carriers, key=lambda k: k['price']),
            stockable=stockable,
            )

@cart.route("/pending", endpoint="cart-pending")
@login_required
@tryton.transaction()
def cart_pending(lang):
    '''Last cart pending'''
    order = [
        ('cart_date', 'DESC'),
        ('id', 'DESC'),
        ]

    domain = [
        ('state', 'in', ['draft', 'wait']),
        ('shop', '=', SHOP),
            ['OR', 
                ('party', '=', session['customer']),
                ('galatea_user', '=', session['user']),
            ]
        ]
    carts = Cart.search_read(
        domain, offset=0, limit=10, order=order, fields_names=CART_FIELD_NAMES)

    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'name': _('Pending'),
        }]

    return render_template('cart-pending.html',
        carts=carts,
        breadcrumbs=breadcrumbs,
    )

@cart.route("/last-products", endpoint="cart-last-products")
@login_required
@tryton.transaction()
def cart_last_products(lang):
    '''Last products'''
    order = [
        ('cart_date', 'DESC'),
        ('id', 'DESC'),
        ]

    domain = [
        ('state', '=', 'done'),
            ['OR', 
                ('party', '=', session['customer']),
                ('galatea_user', '=', session['user']),
            ]
        ]
    cart_products = Cart.search_read(
        domain, offset=0, limit=10, order=order, fields_names=CART_FIELD_NAMES)
    last_product_ids = []
    last_products = []
    for cproduct in cart_products:
        if not cproduct['product_id'] in last_product_ids:
            last_product_ids.append(cproduct['product_id'])
            last_products.append(cproduct)

    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'name': _('Last Products'),
        }]

    return render_template('cart-last-products.html',
        products=last_products,
        breadcrumbs=breadcrumbs,
    )
