from flask import Blueprint, render_template, current_app, g, url_for, \
    flash, redirect, session, request
from galatea.tryton import tryton
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

Cart = tryton.pool.get('sale.cart')
Line = tryton.pool.get('sale.line')
Template = tryton.pool.get('product.template')
Product = tryton.pool.get('product.product')
Address = tryton.pool.get('party.address')
Shop = tryton.pool.get('sale.shop')
Carrier = tryton.pool.get('carrier')
Party = tryton.pool.get('party.party')
Address = tryton.pool.get('party.address')
Sale = tryton.pool.get('sale.sale')
SaleLine = tryton.pool.get('sale.line')

CART_FIELD_NAMES = [
    'cart_date', 'product_id', 'product.rec_name', 'product.template.esale_slug',
    'quantity', 'unit_price', 'untaxed_amount', 'total_amount',
    ]
CART_ORDER = [
    ('cart_date', 'DESC'),
    ('id', 'DESC'),
    ]
from catalog.catalog import CATALOG_FIELD_NAMES

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

    sales = Cart.create_sale(carts, values)
    if not sales:
        current_app.logger.error('Sale. Error create sale party %s' % party.id)
        flash(_('It has not been able to convert the cart into an order. ' \
            'Try again or contact us.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))
    sale, = sales

    # Add shipment line
    product = shop.esale_delivery_product
    shipment_price = Decimal(data.get('carrier-cost'))
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
    for k, v in request.form.iteritems():
        product = k.split('-')
        if product[0] == 'product':
            try:
                values[int(product[1])] = float(v)
            except:
                flash(_('You try to add no numeric quantity. ' \
                    'The request has been stopped.'))
                return redirect(url_for('.cart', lang=g.language))

    # Remove items in cart
    removes = request.form.getlist('remove')

    # Products Current User Cart (products to send)
    products_current_cart = [k for k,v in values.iteritems()]

    # Search current cart by user or session
    domain = [
        ('state', '=', 'draft'),
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
    products = Product.search_read([
        ('id', 'in', products_current_cart),
        ('template.esale_available', '=', True),
        ('template.esale_active', '=', True),
        ('template.esale_saleshops', 'in', SHOPS),
        ], fields_names=['code', 'template.esale_price'])

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
        # Get current product from products
        product_values = None
        for product in products:
            if product_id == product['id']:
                product_values = product
                break

        if not product_values and product_id in products_in_cart: # Remove products cart
            to_remove_products.append(product_id)
            continue

        # Create data
        if product_id not in products_in_cart and qty > 0:
            for product in products:
                if product['id'] == product_id:
                    to_create.append({
                        'party': session.get('customer', None),
                        'quantity': qty,
                        'product': product['id'],
                        'unit_price': product_values['template.esale_price'],
                        'sid': session.sid,
                        'galatea_user': session.get('user', None),
                    })

        # Update data
        if product_id in products_in_cart: 
            for cart in carts:
                if cart.product.id == product_id:
                    if qty > 0:
                        to_update.append({
                            'cart': cart,
                            'values': {
                                'quantity': qty,
                                'unit_price': product_values['template.esale_price'],
                                },
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
    form_shipment_address = ShipmentAddressForm()
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
            values['payment'] = payment
            values['payment_name'] = p.rec_name

    # Carrier
    carrier = int(request.form.get('carrier'))
    for c in shop.esale_carriers:
        if c.id == carrier:
            values['carrier'] = carrier
            values['carrier_name'] = c.rec_name
    values['carrier_cost'] = request.form.get('carrier-cost')

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

    addresses = None
    if session.get('customer'):
        addresses = Address.search([
            ('party', '=', session['customer']),
            ('active', '=', True),
            ], order=[('sequence', 'ASC'), ('id', 'ASC')])

    carriers = []
    for c in shop.esale_carriers:
        carrier_id = c.id
        carrier = Carrier(carrier_id)
        price = carrier.get_sale_price()
        carriers.append({
            'id': carrier_id,
            'name': c.rec_name,
            'price': price[0]
            })

    # Cross Sells
    crossells = []
    if CART_CROSSSELLS:
        product_ids = []
        for cproduct in carts:
            product_ids.append(cproduct['product_id'])
        CATALOG_FIELD_NAMES.append('esale_crosssells')
        products = Template.read(product_ids, CATALOG_FIELD_NAMES)
        crossells_ids = []
        for product in products:
            for crossell in product['esale_crosssells']:
                if not crossell in crossells_ids and len(crossells_ids) < LIMIT_CROSSELLS:
                    crossells_ids.append(crossell)
        if crossells_ids:
            crossells = Template.read(crossells_ids, CATALOG_FIELD_NAMES)

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
            )
