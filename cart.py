from flask import Blueprint, render_template, current_app, abort, g, url_for, \
    flash, redirect, session, request, jsonify
from galatea.tryton import tryton
from galatea.csrf import csrf
from galatea.utils import thumbnail
from galatea.helpers import login_required
from flask.ext.babel import gettext as _, lazy_gettext, ngettext
from flask.ext.wtf import Form
from wtforms import TextField, SelectField, IntegerField, validators
from trytond.transaction import Transaction
from decimal import Decimal
from emailvalid import check_email
import vatnumber

cart = Blueprint('cart', __name__, template_folder='templates')

GALATEA_WEBSITE = current_app.config.get('TRYTON_GALATEA_SITE')
SHOP = current_app.config.get('TRYTON_SALE_SHOP')
SHOPS = current_app.config.get('TRYTON_SALE_SHOPS')
CART_CROSSSELLS = current_app.config.get('TRYTON_CART_CROSSSELLS', True)
LIMIT_CROSSELLS = current_app.config.get('TRYTON_CATALOG_LIMIT_CROSSSELLS', 10)
MINI_CART_CODE = current_app.config.get('TRYTON_CATALOG_MINI_CART_CODE', False)

Website = tryton.pool.get('galatea.website')
Cart = tryton.pool.get('sale.cart')
Template = tryton.pool.get('product.template')
Product = tryton.pool.get('product.product')
Shop = tryton.pool.get('sale.shop')
Carrier = tryton.pool.get('carrier')
Party = tryton.pool.get('party.party')
Address = tryton.pool.get('party.address')
Sale = tryton.pool.get('sale.sale')
SaleLine = tryton.pool.get('sale.line')
Country = tryton.pool.get('country.country')
Subdivision = tryton.pool.get('country.subdivision')

PRODUCT_TYPE_STOCK = ['goods', 'assets']
CART_ORDER = [
    ('cart_date', 'DESC'),
    ('id', 'DESC'),
    ]

VAT_COUNTRIES = [('', '')]
for country in vatnumber.countries():
    VAT_COUNTRIES.append((country, country))

class ShipmentAddressForm(Form):
    "Shipment Address form"
    shipment_name = TextField(lazy_gettext('Name'), [validators.Required()])
    shipment_street = TextField(lazy_gettext('Street'), [validators.Required()])
    shipment_city = TextField(lazy_gettext('City'), [validators.Required()])
    shipment_zip = TextField(lazy_gettext('Zip'), [validators.Required()])
    shipment_country = SelectField(lazy_gettext('Country'), [validators.Required(), ], coerce=int)
    shipment_subdivision = IntegerField(lazy_gettext('Subdivision'), [validators.Required()])
    shipment_email = TextField(lazy_gettext('E-mail'), [validators.Required(), validators.Email()])
    shipment_phone = TextField(lazy_gettext('Phone'))
    vat_country = SelectField(lazy_gettext('VAT Country'), [validators.Required(), ])
    vat_number = TextField(lazy_gettext('VAT Number'), [validators.Required()])

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

    carts = Cart.search(domain, order=CART_ORDER)

    decimals = "%0."+str(shop.esale_currency.digits)+"f" # "%0.2f" euro
    for cart in carts:
        img = cart.product.template.esale_default_images
        image = current_app.config.get('BASE_IMAGE')
        if img.get('small'):
            thumbname = img['small']['name']
            filename = img['small']['digest']
            image = thumbnail(filename, thumbname, '200x200')
        items.append({
            'id': cart.id,
            'name': cart.product.code if MINI_CART_CODE else cart.product.rec_name,
            'url': url_for('catalog.product_'+g.language, lang=g.language,
                slug=cart.product.template.esale_slug),
            'quantity': cart.quantity,
            'unit_price': float(Decimal(decimals % cart.unit_price)),
            'unit_price_w_tax': float(Decimal(decimals % cart.unit_price_w_tax)),
            'untaxed_amount': float(Decimal(decimals % cart.untaxed_amount)),
            'amount_w_tax': float(Decimal(decimals % cart.amount_w_tax)),
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
        if not session.get('logged_in') and session.get('customer'):
            session.pop('customer', None)
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
    try:
        Sale.quote([sale])
    except Exception as e:
        current_app.logger.info(e)

    if current_app.debug:
        current_app.logger.info('Sale. Create sale %s' % sale.id)

    flash(_('Successfully created  Sale Order.'), 'success')

    return redirect(url_for('sale.sale', lang=g.language, id=sale.id))

@csrf.exempt
@cart.route("/add/", methods=["POST"], endpoint="add")
@tryton.transaction()
def add(lang):
    '''Add product item cart'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

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
                prod = data.get('name').split('-')
                try:
                    qty = float(data.get('value'))
                except:
                    qty = 1
                try:
                    values[int(prod[1])] = qty
                except:
                    values[prod[1]] = qty
                    codes.append(prod[1])

        if not values:
            return jsonify(result=False)
    # post request
    else:
        for k, v in request.form.iteritems():
            prod = k.split('-')
            if prod[0] == 'product':
                try:
                    qty = float(v)
                except:
                    flash(_('You try to add no numeric quantity. ' \
                        'The request has been stopped.'))
                    return redirect(url_for('.cart', lang=g.language))
                try:
                    values[int(prod[1])] = qty
                except:
                    values[prod[1]] = qty
                    codes.append(prod[1])

    # transform product code to id
    if codes:
        products = Product.search([('code', 'in', codes)])
        # reset dict
        vals = values.copy()
        values = {}

        for k, v in vals.items():
            for prod in products:
                if prod.code == k:
                    values[prod['id']] = v
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
        ('template.esale_saleshops', 'in', [SHOP]),
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
        product = None
        for p in products:
            if p.id == product_id:
                product = p
                break

        if not product or not product.add_cart:
            continue

        # Add cart if have stock
        if website.esale_stock:
            if website.esale_stock_qty == 'forecast_quantity':
                quantity = product.esale_forecast_quantity
            else:
                quantity = product.esale_quantity
            if product.type in PRODUCT_TYPE_STOCK and not (quantity > 0 and qty <= quantity):
                flash(_('Not enought stock for the product "{product}" (maximun: {quantity} units).').format(
                    product=product.rec_name, quantity=quantity), 'danger')
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
        flash(ngettext(
            '%(num)s product has been added in your cart.',
            '%(num)s products have been added in your cart.',
            len(to_create)), 'success')

    # Update Cart
    if to_update:
        for update in to_update:
            Cart.write([update['cart']], update['values'])
        total = len(to_update)
        if to_remove:
            total = total-len(to_remove)
        flash(ngettext(
            '%(num)s product has been updated in your cart.',
            '%(num)s products have been updated in your cart.',
            len(to_update)), 'success')

    # Delete Cart
    if to_remove:
        Cart.delete(to_remove)
        flash(ngettext(
            '%(num)s product has been deleted in your cart.',
            '%(num)s products have been deleted in your cart.',
            len(to_remove)), 'success')

    if request.json:
        # Add JSON messages (success, warning)
        success = []
        warning = []
        for f in session.get('_flashes', []):
            if f[0] == 'success':
                success.append(f[1])
            else:
                warning.append(f[1])
        messages = {}
        messages['success'] = ",".join(success)
        messages['warning'] = ",".join(warning)

        session.pop('_flashes', None)
        return jsonify(result=True, messages=messages)
    else:
        return redirect(url_for('.cart', lang=g.language))

@cart.route("/checkout/", methods=["GET", "POST"], endpoint="checkout")
@tryton.transaction()
def checkout(lang):
    '''Checkout user or session'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

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
    carts = Cart.search(domain, order=CART_ORDER)
    if not carts:
        flash(_('There are not products in your cart.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    untaxed_amount = Decimal(0)
    tax_amount = Decimal(0)
    total_amount = Decimal(0)
    for cart in carts:
        untaxed_amount += cart.untaxed_amount
        tax_amount += cart.amount_w_tax - cart.untaxed_amount
        total_amount += cart.amount_w_tax
        # checkout stock available
        if website.esale_stock:
            if cart.product.type not in PRODUCT_TYPE_STOCK:
                continue
            if website.esale_stock_qty == 'forecast_quantity':
                quantity = cart.product.esale_forecast_quantity
            else:
                quantity = cart.product.esale_quantity
            if not (cart.quantity > 0 and cart.quantity <= quantity):
                flash(_('Not enought stock for the product "{product}" (maximun: {quantity} units).').format(
                    product=cart.product.rec_name, quantity=quantity), 'danger')
                return redirect(url_for('.cart', lang=g.language))

    party = None
    if session.get('customer'):
        party = Party(session.get('customer'))

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
        values['shipment_phone'] = request.form.get('shipment_phone')

        if session.get('email'):
            values['shipment_email'] = session['email']
        else:
            shipment_email = request.form.get('shipment_email')
            if not check_email(shipment_email):
                errors.append(_('Email not valid.'))
            values['shipment_email'] = shipment_email

        shipment_country = request.form.get('shipment_country')
        if shipment_country:
            values['shipment_country'] = shipment_country
            country, = Country.browse([shipment_country])
            values['shipment_country_name'] = country.name

        shipment_subdivision = request.form.get('shipment_subdivision')
        if shipment_subdivision:
            values['shipment_subdivision'] = shipment_subdivision
            subdivision, = Subdivision.browse([shipment_subdivision])
            values['shipment_subdivision_name'] = subdivision.name

        if not values['shipment_name'] or not values['shipment_street'] \
                or not values['shipment_zip'] or not values['shipment_city'] \
                or not values['shipment_email']:
            errors.append(_('Error when validate Shipment Address. ' \
                'Please, return to cart and complete Shipment Address'))

        vat_country = request.form.get('vat_country')
        vat_number = request.form.get('vat_number')

        if vat_number:
            values['vat_number'] = vat_number
            if vat_country:
                values['vat_country'] = vat_country

        if vat_country and vat_number:
            vat_number = '%s%s' % (vat_country.upper(), vat_number)
            if not vatnumber.check_vat(vat_number):
                errors.append(_('VAT not valid.'))
    elif party:
        addresses = Address.search([
            ('party', '=', party),
            ('id', '=', int(shipment_address)),
            ('active', '=', True),
            ], order=[('sequence', 'ASC'), ('id', 'ASC')])
        if addresses:
            address, = addresses
            values['shipment_address_name'] = address.full_address
        else:
            errors.append(_('We can found address related yours address. ' \
                'Please, select a new address in Shipment Address'))
    else:
        errors.append(_('You not select new address and not a customer. ' \
            'Please, select a new address in Shipment Address'))

    # Payment
    payment = int(request.form.get('payment'))
    payment_type = None
    if party and hasattr(party, 'customer_payment_type'):
        if party.customer_payment_type:
            payment_type = party.customer_payment_type
            values['payment'] = payment_type.id
            values['payment_name'] = payment_type.rec_name
    if not payment_type:
        for p in shop.esale_payments:
            if p.payment_type.id == payment:
                payment_type = p.payment_type
                values['payment'] = payment_type.id
                values['payment_name'] = payment_type.rec_name
                break

    # Carrier
    carrier_id = request.form.get('carrier')
    if carrier_id:
        carrier = Carrier(carrier_id)

        # create a virtual sale
        sale = Sale()
        sale.untaxed_amount = untaxed_amount
        sale.tax_amount = tax_amount
        sale.total_amount = total_amount
        sale.carrier = carrier
        sale.payment_type = payment_type

        context = {}
        context['record'] = sale # Eval by "carrier formula" require "record"
        context['carrier'] = carrier
        with Transaction().set_context(context):
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
        }, {
        'slug': url_for('.cart', lang=g.language),
        'name': _('Checkout'),
        }]

    return render_template('checkout.html',
            website=website,
            breadcrumbs=breadcrumbs,
            shop=shop,
            carts=carts,
            values=values,
            errors=errors,
            prices={
                'untaxed_amount': untaxed_amount,
                'tax_amount': tax_amount,
                'total_amount': total_amount,
                },
            )

@cart.route("/", endpoint="cart")
@tryton.transaction()
def cart_list(lang):
    '''Cart by user or session'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

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
    carts = Cart.search(domain, order=CART_ORDER)

    products = []
    untaxed_amount = Decimal(0)
    tax_amount = Decimal(0)
    total_amount = Decimal(0)
    for cart in carts:
        products.append(cart.product.id)
        untaxed_amount += cart.untaxed_amount
        tax_amount += cart.amount_w_tax - cart.untaxed_amount
        total_amount += cart.amount_w_tax

    party = None
    addresses = []
    delivery_addresses = []
    invoice_addresses = []
    if session.get('customer'):
        party = Party(session.get('customer'))
        for address in party.addresses:
            addresses.append(address)
            if address.delivery:
                delivery_addresses.append(address)
            if address.invoice:
                invoice_addresses.append(address)

    # Get payments. Shop payments or Party payment
    payments = []
    default_payment = None
    if shop.esale_payments:
        default_payment = shop.esale_payments[0].payment_type
        if party:
            if hasattr(party, 'customer_payment_type'):
                if party.customer_payment_type:
                    payments = [party.customer_payment_type]
        if not payments:
            payments = [payment.payment_type for payment in shop.esale_payments]

    # Get carriers. Shop carriers or Party carrier
    stockable = Carrier.get_products_stockable(products)
    carriers = []
    if stockable:
        # create a virtual sale
        sale = Sale()
        sale.untaxed_amount = untaxed_amount
        sale.tax_amount = tax_amount
        sale.total_amount = total_amount
        sale.payment_type = default_payment

        context = {}
        context['record'] = sale # Eval by "carrier formula" require "record"

        if party:
            if hasattr(party, 'carrier'):
                carrier = party.carrier
                sale.carrier = carrier
                if carrier:
                    context['carrier'] = carrier
                    with Transaction().set_context(context):
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
                sale.carrier = carrier
                context['carrier'] = carrier
                with Transaction().set_context(context):
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
        template_ids = list({c.product.template.id for c in carts})
        templates = Template.browse(template_ids)
        crossells_ids = set()
        for template in templates:
            for crossell in template.esale_crosssells_by_shop:
                crossells_ids.add(crossell.id)
        if crossells_ids:
            crossells = Template.browse(list(crossells_ids)[:LIMIT_CROSSELLS])

    # Breadcumbs
    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }]

    return render_template('cart.html',
            website=website,
            breadcrumbs=breadcrumbs,
            shop=shop,
            carts=carts,
            form_shipment_address=form_shipment_address,
            addresses=addresses,
            delivery_addresses=delivery_addresses,
            invoice_addresses=invoice_addresses,
            crossells=crossells,
            payments=payments,
            carriers=sorted(carriers, key=lambda k: k['price']),
            stockable=stockable,
            prices={
                'untaxed_amount': untaxed_amount,
                'tax_amount': tax_amount,
                'total_amount': total_amount,
                },
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
    carts = Cart.search(domain, offset=0, limit=10, order=order)

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
    cart_products = Cart.search(domain, offset=0, limit=10, order=order)
    last_products = set()
    for cproduct in cart_products:
        last_products.add(cproduct)

    breadcrumbs = [{
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }, {
        'name': _('Last Products'),
        }]

    return render_template('cart-last-products.html',
        products=list(last_products),
        breadcrumbs=breadcrumbs,
    )
