import logging
import csv
import StringIO

from ckan import model
import ckan.new_authz as authz
from ckan.lib.base import c
import ckan.plugins as p
from ckan.lib.navl.dictization_functions import validate

from ckanext.iati.logic.validators import (iati_dataset_name_from_csv,
                                           file_type_validator,
                                           date_from_csv,
                                           yes_no,
                                           country_code)
from ckanext.iati.logic.converters import strip

log = logging.getLogger(__name__)

_not_empty = p.toolkit.get_validator('not_empty')
_ignore_empty = p.toolkit.get_validator('ignore_empty')
_ignore_missing = p.toolkit.get_validator('ignore_missing')
_int_validator = p.toolkit.get_validator('int_validator')

CSV_MAPPING = [
        ('registry-publisher-id', 'organization', 'name', [_not_empty]),
        ('registry-file-id', 'package', 'name', [_not_empty, iati_dataset_name_from_csv]),
        ('title', 'package', 'title', []),
        ('contact-email', 'package', 'author_email', []),
        ('state', 'package', 'state', [_ignore_missing]),
        ('source-url', 'resources', 'url', []),
        ('format', 'resources', 'format', []),
        ('file-type','extras', 'filetype', [_ignore_empty, file_type_validator]),
        ('recipient-country','extras', 'country', [_ignore_empty, country_code]),
        ('activity-period-start','extras', 'activity_period-from', [_ignore_empty, date_from_csv]),
        ('activity-period-end','extras', 'activity_period-to', [_ignore_empty, date_from_csv]),
        ('last-updated-datetime','extras', 'data_updated', [_ignore_empty, date_from_csv]),
        ('activity-count','extras', 'activity_count', [_ignore_empty,_int_validator]),
        ('verification-status','extras', 'verified', [_ignore_empty,yes_no]),
        ('default-language','extras', 'language', []),
        ('secondary-publisher', 'extras', 'secondary_publisher', [strip]),
        ]

class CSVController(p.toolkit.BaseController):


    def __before__(self, action, **params):
        super(CSVController,self).__before__(action, **params)

        if not c.user:
            p.toolkit.abort(403,'Permission denied')

        self.is_sysadmin = authz.is_sysadmin(c.user)

        # Orgs of which the logged user is admin
        context = {'model': model, 'user': c.user or c.author}
        self.authz_orgs = p.toolkit.get_action('organization_list_for_user')(context, c.user)

    def download(self, publisher=None):

        context = {'model': model, 'user': c.user or c.author}

        if publisher and publisher not in ['all','template']:
            try:
                org = p.toolkit.get_action('organization_show')(context, {'id': publisher})
            except p.toolkit.ObjectNotFound:
                p.toolkit.abort(404, 'Publisher not found')

            if not org['id'] in self.authz_orgs and not self.is_sysadmin:
                p.toolkit.abort(403,'Permission denied for this publisher organization')

        if self.is_sysadmin:
            if publisher:
                # Return CSV for provided publisher
                output = self.write_csv_file(publisher)
            else:
                # Show list of all available publishers
                orgs = p.toolkit.get_action('organization_list')(context, {'all_fields': True})
                return p.toolkit.render('csv/index.html', extra_vars={'orgs': orgs})
        else:
            if publisher and publisher != 'all':
                # Return CSV for provided publisher (we already checked the permissions)
                output = self.write_csv_file(publisher)
            elif len(self.authz_orgs) == 1:
                # Return directly CSV for publisher
                output = self.write_csv_file(self.authz_orgs[0])
            elif len(self.authz_orgs) > 1:
                # Show list of available publishers for this user
                return p.toolkit.render('csv/index.html', extra_vars={'orgs', self.authz_orgs})
            else:
                # User does not have permissions on any publisher
                p.toolkit.abort(403, 'Permission denied')


        file_name = publisher if publisher else 'iati-registry-records'
        p.toolkit.response.headers['Content-type'] = 'text/csv'
        p.toolkit.response.headers['Content-disposition'] = 'attachment;filename=%s.csv' % str(file_name)
        return output

    def upload(self):
        if not self.is_sysadmin and not self.authz_orgs:
            # User does not have permissions on any publisher
            p.toolkit.abort(403,'Permission denied')

        if p.toolkit.request.method == 'GET':
            return p.toolkit.render('csv/upload.html')
        elif p.toolkit.request.method == 'POST':
            csv_file = p.toolkit.request.POST['file']

            if not hasattr(csv_file,'filename'):
                p.toolkit.abort(400,'No CSV file provided')
            vars = {}
            vars['file_name'] = csv_file.filename

            added, updated, warnings, errors = self.read_csv_file(csv_file.file)
            vars['added'] = added
            vars['updated'] = updated

            vars['errors'] = errors
            vars['warnings'] = warnings

            log.info('CSV import finished: file %s, %i added, %i updated, %i warnings, %i errors' % \
                    (vars['file_name'],len(vars['added']),len(vars['updated']),len(vars['warnings']),len(vars['errors'])))

            return p.toolkit.render('csv/result.html', extra_vars=vars)

    def write_csv_file(self, publisher):
        context = {'model': model, 'user': c.user or c.author}
        try:
            if publisher == 'all':
                packages = p.toolkit.get_action('package_list')(context, {})
            elif publisher == 'template':
                # Just return an empty CSV file with just the headers
                packages = []
            else:
                org = p.toolkit.get_action('organization_show')(context, {'id': publisher})
                packages = [pkg['name'] for pkg in org['packages']]
        except p.toolkit.ObjectNotFound:
            p.toolkit.abort(404, 'Organization not found')

        f = StringIO.StringIO()

        output = ''
        try:
            fieldnames = [n[0] for n in CSV_MAPPING if n[0] != 'state']
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            headers = dict( (n[0],n[0]) for n in CSV_MAPPING if n[0] != 'state')
            writer.writerow(headers)

            packages.sort()
            for pkg in packages:
                try:
                    package = p.toolkit.get_action('package_show')(context, {'id': pkg})
                    package.pop('state', None)
                except p.toolkit.NotAuthorized:
                    log.warn('User %s not authorized to read package %s' % (c.user, pkg))
                    continue
                if package:
                    row = {}
                    for fieldname, entity, key, v in CSV_MAPPING:
                        if key == 'state':
                            continue
                        value = None
                        if entity == 'organization':
                            if len(package['organization']):
                                value = package['organization']['name']
                        elif entity == 'resources':
                            if len(package['resources']) and key in package['resources'][0]:
                                value = package['resources'][0][key]
                        elif entity == 'extras':
                            if key in package['extras']:
                                value = package['extras'][key]
                        else:
                            if key in package:
                                value = package[key]
                        row[fieldname] = value

                        if fieldname == 'title':
                            row['title'] = row['title'].encode('utf-8')
                    writer.writerow(row)
            output = f.getvalue()
        finally:
            f.close()

        return output

    def read_csv_file(self,csv_file,context=None):
        fieldnames = [f[0] for f in CSV_MAPPING]

        warnings = {}

        try:
            # Try to sniff the file dialect
            dialect = csv.Sniffer().sniff(csv_file.read(1024),delimiters=[',',';','\t'])
        except csv.Error:
            # If there was an error, I'll bet you a pint it's an Excel file
            dialect = csv.excel

        csv_file.seek(0)
        try:
            reader = csv.DictReader(csv_file, dialect=dialect)

            # Check if all columns are present
            missing_columns = [f for f in fieldnames if f not in reader.fieldnames and f != 'state']
            if len(missing_columns):
                error = {'file': 'Missing columns: %s' % ', '.join(sorted(missing_columns))}
                return [], [], [], [('1',error)]

            surplus_columns = [f for f in reader.fieldnames if f not in fieldnames]
            if len(surplus_columns):
                warnings['1'] = {}
                warnings['1']['file'] = 'Ignoring extra columns: %s' % ', '.join(sorted(surplus_columns))

        except csv.Error,e:
            error = {'file': 'Error reading CSV file: %s' % str(e)}
            return [], [], [], [('1',error)]


        log.debug('Starting reading CSV file (delimiter "%s", escapechar "%s")' %
                    (dialect.delimiter,dialect.escapechar))

        if not context:
            context = {'model':model, 'session':model.Session, 'user': c.user or c.author, 'api_version':'1'}

        counts = {'added': [], 'updated': []}
        errors = {}
        for i,row in enumerate(reader):
            row_index = str(i + 1)
            errors[row_index] = {}
            try:
                # We will now run the IATI specific validation, CKAN core will
                # run the default one later on
                schema = dict([(f[0],f[3]) for f in CSV_MAPPING])
                row, row_errors = validate(row,schema,context)
                if row_errors:
                    for key, msgs in row_errors.iteritems():
                        log.error('Error in row %i: %s: %s' % (i+1,key,str(msgs)))
                        errors[row_index][key] = msgs
                    continue

                package_dict = self.get_package_dict_from_row(row)
                self.create_or_update_package(package_dict,counts,context=context)

                del errors[row_index]
            except p.toolkit.ValidationError,e:
                iati_keys = dict([(f[2],f[0]) for f in CSV_MAPPING])
                for key, msgs in e.error_dict.iteritems():
                    iati_key = iati_keys[key]
                    log.error('Error in row %i: %s: %s' % (i+1,iati_key,str(msgs)))
                    errors[row_index][iati_key] = msgs
            except p.toolkit.NotAuthorized,e:
                msg = 'Not authorized to publish to this organization: %s' % row['registry-publisher-id']
                log.error('Error in row %i: %s' % (i+1,msg))
                errors[row_index]['registry-publisher-id'] = [msg]
            except p.toolkit.ObjectNotFound,e:
                msg = 'Publisher not found: %s' % row['registry-publisher-id']
                log.error('Error in row %i: %s' % (i+1,msg))
                errors[row_index]['registry-publisher-id'] = [msg]


        warnings = sorted(warnings.iteritems())
        errors = sorted(errors.iteritems())
        return counts['added'], counts['updated'], warnings, errors

    def get_package_dict_from_row(self,row):
        context = {'model': model, 'user': c.user or c.author}
        package = {}
        for fieldname, entity, key, v in CSV_MAPPING:
            if fieldname in row:
                # If value is None (empty cell), property will be set to blank
                value = row[fieldname]
                if entity == 'organization':
                    org = p.toolkit.get_action('organization_show')(context, {'id': value})
                    package['owner_org'] = org['id']
                elif entity == 'resources':
                    if not 'resources' in package:
                       package['resources'] = [{}]
                    package['resources'][0][key] = value
                elif entity == 'extras':
                    if not 'extras' in package:
                       package['extras'] = {}
                    package['extras'][key] = value
                else:
                    package[key] = value
        return package

    def create_or_update_package(self, package_dict, counts = None, context = None):
        if not context:
            context = {
                'model': model,
                'session': model.Session,
                'user': c.user,
                'api_version':'1'
            }

        # Check if package exists
        data_dict = {}
        data_dict['id'] = package_dict['name']

        package_dict['title'] = package_dict['title'].decode('utf-8')
        try:
            existing_package_dict = p.toolkit.get_action('package_show')(context, data_dict)

            # Update package
            log.info('Package with name "%s" exists and will be updated' % package_dict['name'])

            context.update({'id':existing_package_dict['id']})
            package_dict.update({'id':existing_package_dict['id']})

            context['message'] = 'CSV import: update dataset %s' % package_dict['name']

            updated_package = p.toolkit.get_action('package_update_rest')(context, package_dict)
            if counts:
                counts['updated'].append(updated_package['name'])
            log.debug('Package with name "%s" updated' % package_dict['name'])
        except p.toolkit.ObjectNotFound:
            # Package needs to be created
            log.info('Package with name "%s" does not exist and will be created' % package_dict['name'])

            context['message'] = 'CSV import: create dataset %s' % package_dict['name']

            new_package = p.toolkit.get_action('package_create_rest')(context, package_dict)
            if counts:
                counts['added'].append(new_package['name'])
            log.debug('Package with name "%s" created' % package_dict['name'])

