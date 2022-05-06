
# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.

from trytond.modules.company.tests import CompanyTestMixin
from trytond.tests.test_tryton import ModuleTestCase


class AccountPaymentProcessingTestCase(CompanyTestMixin, ModuleTestCase):
    'Test AccountPaymentProcessing module'
    module = 'account_payment_processing'
    extras = ['account_bank_statement_payment']


del ModuleTestCase
