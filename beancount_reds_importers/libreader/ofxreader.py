"""Ofx importer module for beancount to be used along with investment/banking/other importer modules in
beancount_reds_importers."""

import datetime
import ofxparse
from collections import namedtuple
from beancount.ingest import importer
from beancount_reds_importers.libreader import reader
from bs4.builder import XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


class Importer(reader.Reader, importer.ImporterProtocol):
    FILE_EXTS = ['ofx', 'qfx']

    def initialize_reader(self, file):
        if getattr(self, 'file', None) != file:
            self.file = file
            self.ofx_account = None
            self.reader_ready = False
            try:
                self.ofx = self.read_file(file)
            except ofxparse.OfxParserException:
                return
            for acc in self.ofx.accounts:
                # account identifying info fieldname varies across institutions
                # self.acc_num_field can be overridden in self.custom_init() if needed
                acc_num_field = getattr(self, 'account_number_field', 'account_id')
                if self.match_account_number(getattr(acc, acc_num_field),
                                             self.config['account_number']):
                    self.ofx_account = acc
                    self.reader_ready = True
            if self.reader_ready:
                self.currency = self.ofx_account.statement.currency.upper()

    def match_account_number(self, file_account, config_account):
        """We many not want to store entire credit card numbers in our config. Or a given ofx may not contain
        the full account number. Override this method to handle these cases."""
        return file_account == config_account

    def file_date(self, file):
        """Get the ending date of the statement."""
        if not getattr(self, 'ofx_account', None):
            self.initialize(file)
        try:
            return self.ofx_account.statement.end_date
        except AttributeError:
            return None

    def read_file(self, file):
        with open(file.name) as fh:
            return ofxparse.OfxParser.parse(fh)

    def get_transactions(self):
        yield from self.ofx_account.statement.transactions

    def get_balance_statement(self, file=None):
        if not hasattr(self.ofx_account.statement, 'balance'):
            return []
        date = self.get_balance_assertion_date()
        if date:
            Balance = namedtuple('Balance', ['date', 'amount'])
            yield Balance(date, self.ofx_account.statement.balance)

    def get_balance_positions(self):
        if not hasattr(self.ofx_account.statement, 'positions'):
            return []
        yield from self.ofx_account.statement.positions

    def get_available_cash(self, settlement_fund_balance=0):
        available_cash = getattr(self.ofx_account.statement, 'available_cash', None)
        if available_cash is not None:
            # Some institutions compute available_cash this way. For others, override this method
            # in the importer
            return available_cash - settlement_fund_balance
        return None

    def get_ofx_end_date(self):
        end_date = self.ofx_account.statement.end_date
        # convert end_date from utc to local timezone
        end_date = end_date.replace(tzinfo=datetime.timezone.utc).astimezone().date()
        return end_date

    def get_smart_date(self):
        """ We find the statement's end date from the OFX file. However, banks and credit cards
        typically have pending transactions that are not included in downloads. When we download
        the next statement, new transactions may appear prior to the balance assertion date that we
        generate for this statement. To attempt to avoid this, we set the balance assertion date to
        either two days before the statement's end date or the last transaction's date, whichever
        is later.
        """
        end_date = self.get_ofx_end_date()
        end_date -= datetime.timedelta(days=self.config.get('balance_assertion_date_fudge', 2))

        max_transaction_date = self.get_max_transaction_date()
        max_transaction_date = max_transaction_date if max_transaction_date else datetime.date.min
        return_date = max(end_date, max_transaction_date)
        return return_date

    def get_balance_assertion_date(self):
        """ Choices for the date of the generated balance assertion can be specified in
        self.config['balance_assertion_date_type'], which can be:
        - 'smart':            smart date (default)
        - 'ofx_date':         date specified in ofx file
        - 'last_transaction': max transaction date
        - 'today':            today's date

        If you want something else, simply override this method in individual importer

        Finally, we add an additional day, since Beancount balance assertions are defined to occur
        on the beginning of the assertion date.
        """

        date_type_map = {'smart': self.get_smart_date,
                         'ofx_date': self.get_ofx_end_date,
                         'last_transaction': self.get_max_transaction_date,
                         'today': datetime.date.today}
        date_type = self.config.get('balance_assertion_date_type', 'smart')
        return_date = date_type_map[date_type]()

        return return_date + datetime.timedelta(days=1)  # Next day, as defined by Beancount

    def get_max_transaction_date(self):
        """
        Here, we find the last transaction's date. If we use the ofx download date (if our source is ofx), we
        could end up with a gap in time between the last transaction's date and balance assertion.
        Pending (but not yet downloaded) transactions in this gap will get downloaded the next time we
        do a download in the future, and cause the balance assertions to be invalid. This is
        a problem particularly with credit card accounts and bank accounts.

        """
        try:

            date = max(ot.tradeDate if hasattr(ot, 'tradeDate') else ot.date
                       for ot in self.get_transactions()).date()
        except TypeError:
            return False
        except ValueError:
            return False
        return date
