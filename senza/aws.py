import base64
import boto3
import collections
import datetime
import functools
import re
import time
import yaml
from botocore.exceptions import ClientError
from click import FileError

from .stack_references import check_file_exceptions


def resolve_referenced_resource(ref: dict, region: str):
    if 'Stack' in ref and 'LogicalId' in ref:
        cf = boto3.client('cloudformation', region)
        resource = cf.describe_stack_resource(
            StackName=ref['Stack'],
            LogicalResourceId=ref['LogicalId'])['StackResourceDetail']
        if not is_status_complete(resource['ResourceStatus']):
            raise ValueError('Resource "{}" is not ready ("{}")'.format(ref['LogicalId'], resource['ResourceStatus']))

        resource_id = resource['PhysicalResourceId']

        # sg is referenced by its name not its id
        if resource['ResourceType'] == 'AWS::EC2::SecurityGroup':
            sg = get_security_group(region, resource_id)
            return sg.id if sg is not None else None
        else:
            return resource_id
    elif 'Stack' in ref and 'Output' in ref:
        cf = boto3.client('cloudformation', region)
        stack = cf.describe_stacks(
            StackName=ref['Stack'])['Stacks'][0]
        if not is_status_complete(stack['StackStatus']):
            raise ValueError('Stack "{}" is not ready ("{}")'.format(ref['Stack'], stack['StackStatus']))

        for output in stack.get('Outputs', []):
            if output['OutputKey'] == ref['Output']:
                return output['OutputValue']

        return None
    else:
        return ref


def is_status_complete(status: str):
    return status in ('CREATE_COMPLETE', 'UPDATE_COMPLETE')


def get_security_group(region: str, sg_name: str):
    ec2 = boto3.resource('ec2', region)
    try:
        sec_groups = list(ec2.security_groups.filter(
            Filters=[{'Name': 'group-name', 'Values': [sg_name]}]
        ))
        if not sec_groups:
            return None
        # FIXME: What if we have 2 VPC, with a SG with the same name?!
        return sec_groups[0]
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
            return None
        elif e.response['Error']['Code'] == 'VPCIdNotSpecified':
            # no Default VPC, we must use the lng way...
            for sg in ec2.security_groups.all():
                # FIXME: What if we have 2 VPC, with a SG with the same name?!
                if sg.group_name == sg_name:
                    return sg
            return None
        else:
            raise


def get_vpc_attribute(region: str, vpc_id: str, attribute: str):
    ec2 = boto3.resource('ec2', region)
    vpc = ec2.Vpc(vpc_id)

    return getattr(vpc, attribute, None)


def encrypt(region: str, KeyId: str, Plaintext: str, b64encode=False):
    kms = boto3.client('kms', region)
    encrypted = kms.encrypt(KeyId=KeyId, Plaintext=Plaintext)['CiphertextBlob']
    if b64encode:
        return base64.b64encode(encrypted).decode('utf-8')

    return encrypted


def list_kms_keys(region: str, details=True):
    kms = boto3.client('kms', region)
    keys = list(kms.list_keys()['Keys'])
    if details:
        aliases = kms.list_aliases()['Aliases']

        for key in keys:
            key['aliases'] = [a['AliasName'] for a in aliases if a.get('TargetKeyId') == key['KeyId']]
            key.update(kms.describe_key(KeyId=key['KeyId'])['KeyMetadata'])

    return keys


def resolve_security_group(security_group, region: str):
    if isinstance(security_group, dict):
        sg = resolve_referenced_resource(security_group, region)
        if not sg:
            raise ValueError('Referenced Security Group "{}" does not exist'.format(security_group))
        return sg
    elif security_group.startswith('sg-'):
        return security_group
    else:
        sg = get_security_group(region, security_group)
        if not sg:
            raise ValueError('Security Group "{}" does not exist'.format(security_group))
        return sg.id


def resolve_security_groups(security_groups: list, region: str):
    result = []
    for security_group in security_groups:
        result.append(resolve_security_group(security_group, region))
    return result


def parse_time(s: str) -> float:
    '''
    >>> parse_time('2015-04-14T19:09:01.000Z') > 0
    True
    '''
    try:
        utc = datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%fZ')
        ts = time.time()
        utc_offset = datetime.datetime.fromtimestamp(ts) - datetime.datetime.utcfromtimestamp(ts)
        local = utc + utc_offset
        return local.timestamp()
    except:
        return None


def get_required_capabilities(data: dict):
    '''Get capabilities for a given cloud formation template for the "create_stack" call

    >>> get_required_capabilities({})
    []

    >>> get_required_capabilities({'Resources': {'MyRole': {'Type': 'AWS::IAM::Role', 'a': 'b'}}})
    ['CAPABILITY_IAM']
    '''
    capabilities = []
    for logical_id, config in data.get('Resources', {}).items():
        if config.get('Type').startswith('AWS::IAM'):
            capabilities.append('CAPABILITY_IAM')
    return capabilities


def resolve_topic_arn(region, topic_name):
    '''
    >>> resolve_topic_arn(None, 'arn:123')
    'arn:123'
    '''
    topic_arn = None
    if topic_name.startswith('arn:'):
        topic_arn = topic_name
    else:
        # resolve topic name to ARN
        sns = boto3.resource('sns', region)
        for topic in sns.topics.all():
            if topic.arn.endswith(':{}'.format(topic_name)):
                topic_arn = topic.arn

    return topic_arn


@functools.total_ordering
class SenzaStackSummary:
    def __init__(self, stack):
        self.stack = stack
        parts = stack['StackName'].rsplit('-', 1)
        self.name = parts[0]
        if len(parts) > 1:
            self.version = parts[1]
        else:
            self.version = ''

    def __getattr__(self, item):
        if item in self.__dict__:
            return self.__dict__[item]
        return self.stack.get(item)

    def __lt__(self, other):
        def key(v):
            return (v.name, v.version)
        return key(self) < key(other)

    def __eq__(self, other):
        return self.stack['StackName'] == other.stack['StackName']


def get_stacks(stack_refs: list, region, all=False, unique_only=False):
    # boto3.resource('cf')-stacks.filter() doesn't support status_filter, only StackName
    cf = boto3.client('cloudformation', region)
    if all:
        status_filter = []
    else:
        # status_filter = [st for st in cf.valid_states if st != 'DELETE_COMPLETE']
        status_filter = [
            "CREATE_IN_PROGRESS",
            "CREATE_FAILED",
            "CREATE_COMPLETE",
            "ROLLBACK_IN_PROGRESS",
            "ROLLBACK_FAILED",
            "ROLLBACK_COMPLETE",
            "DELETE_IN_PROGRESS",
            "DELETE_FAILED",
            # "DELETE_COMPLETE",
            "UPDATE_IN_PROGRESS",
            "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_IN_PROGRESS",
            "UPDATE_ROLLBACK_FAILED",
            "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
            "UPDATE_ROLLBACK_COMPLETE"
        ]
    kwargs = {'StackStatusFilter': status_filter}
    stacks = []
    while 'NextToken' not in kwargs or kwargs['NextToken']:
        results = cf.list_stacks(**kwargs)
        for stack in results['StackSummaries']:
            if not stack_refs or matches_any(stack['StackName'], stack_refs):
                stacks.append(stack)
        kwargs['NextToken'] = results.get('NextToken')
    # After going through all stacks
    check_file_exceptions(stack_refs)

    stacks.sort(key=lambda x: x['CreationTime'], reverse=True)
    # stack names that were already yielded to avoid yielding old deleted
    # stacks whose name was reused
    stacks_yielded = set()
    for stack in stacks:
        if stack['StackName'] not in stacks_yielded or not unique_only:
            stacks_yielded.add(stack['StackName'])
            yield SenzaStackSummary(stack)


def matches_any(cf_stack_name: str, stack_refs: list):
    """
    >>> matches_any(None, [StackReference(name='foobar', version=None)])
    False

    >>> matches_any('foobar-1', [])
    False

    >>> matches_any('foobar-1', [StackReference(name='foobar', version=None)])
    True

    >>> matches_any('foobar-1', [StackReference(name='foobar', version='1')])
    True

    >>> matches_any('foobar-1', [StackReference(name='foobar', version='2')])
    False

    >>> matches_any('foobar-1', [StackReference(name='foob.r', version='\d')])
    True
    """

    cf_stack_name = cf_stack_name or ''  # ensure cf_stack_name is a str
    try:
        name, version = cf_stack_name.rsplit('-', 1)
    except ValueError:
        name = cf_stack_name
        version = ""
    return any(ref.matches(name, version) for ref in stack_refs)


def get_tag(tags: list, key: str, default=None):
    '''
    >>> tags = [{'Key': 'aws:cloudformation:stack-id',
    ...          'Value': 'arn:aws:cloudformation:eu-west-1:123:stack/test-123'},
    ...         {'Key': 'Name',
    ...          'Value': 'test-123'},
    ...         {'Key': 'StackVersion',
    ...          'Value': '123'}]
    >>> get_tag(tags, 'StackVersion')
    '123'
    >>> get_tag(tags, 'aws:cloudformation:stack-id')
    'arn:aws:cloudformation:eu-west-1:123:stack/test-123'
    >>> get_tag(tags, 'notfound') is None
    True
    '''
    if isinstance(tags, list):
        found = [tag['Value'] for tag in tags if tag['Key'] == key]
        if len(found):
            return found[0]
    return default


def get_account_id():
    conn = boto3.client('iam')
    try:
        own_user = conn.get_user()['User']
    except:
        own_user = None
    if not own_user:
        roles = conn.list_roles()['Roles']
        if not roles:
            users = conn.list_users()['Users']
            if not users:
                saml = conn.list_saml_providers()['SAMLProviderList']
                if not saml:
                    return None
                else:
                    arn = [s['Arn'] for s in saml][0]
            else:
                arn = [u['Arn'] for u in users][0]
        else:
            arn = [r['Arn'] for r in roles][0]
    else:
        arn = own_user['Arn']
    account_id = arn.split(':')[4]
    return account_id


def get_account_alias():
    conn = boto3.client('iam')
    return conn.list_account_aliases()['AccountAliases'][0]


class StackReference(collections.namedtuple('StackReference', 'name version')):
    def __init__(self, *args, **kwargs):
        self.matched = 0
        self.possible_definition_file = (self.name.endswith('.yml') or
                                         self.name.endswith('.yaml'))

    def raise_file_exception(self):
        """
        If it looks like a filename and didn't match anything try to open it
        to see if exists and can be opened and raise an exception if it can't
        """
        if not self.matched and self.possible_definition_file:
            try:
                with open(self.name) as fd:
                    data = yaml.safe_load(fd)
                ref = data['SenzaInfo']['StackName']
            except (OSError, IOError) as error:
                raise FileError(ref, str(error))
            except KeyError as error:
                raise ValueError("SenzaInfo.StackName missing from definition file")

    def matches(self, name: str, version: str):
        matches_name = re.match(self.name + '$', name)
        matches_version = (not self.version or
                           re.match(self.version + '$', version))
        matches = bool(matches_name and matches_version)
        if matches:
            self.matched += 1
        return matches

    def cf_stack_name(self):
        return ('{}-{}'.format(self.name, self.version)
                if self.version
                else self.name)
