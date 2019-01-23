import shutil
from collections import namedtuple
from glob import glob
from os import chdir, getcwd, listdir, makedirs
from os.path import abspath, exists, isfile, isdir, join, split
from time import sleep

from nephos.fabric.settings import get_namespace
from nephos.fabric.utils import credentials_secret, crypto_secret, get_pod
from nephos.helpers.k8s import ns_create, ingress_read, secret_from_file
from nephos.helpers.misc import execute, execute_until_success

PWD = getcwd()
CryptoInfo = namedtuple('CryptoInfo', ('secret_type', 'subfolder', 'key', 'required'))


# CA Helpers
# TODO: We can probably split the part that checks the identity and the part that registers it
def register_id(ca_namespace, ca, username, password, node_type="client", admin=False, verbose=False):
    # Get CA
    ca_exec = get_pod(namespace=ca_namespace, release=ca, app='hlf-ca', verbose=verbose)
    # Check if Orderer is registered with the relevant CA
    got_id = False
    while not got_id:
        ord_id, err = ca_exec.execute(
            'fabric-ca-client identity list --id {id}'.format(id=username))
        if err:
            # Expected error (identity does not exist)
            if 'no rows in result set' in err:
                got_id = True
            # Otherwise, unexpected error, we are having issues connecting to CA
            else:
                sleep(15)
        else:
            got_id = True
    # Registered if needed
    if not ord_id:
        command = 'fabric-ca-client register --id.name {id} --id.secret {pw} --id.type {type}'
        if admin:
            command += " --id.attrs 'admin=true:ecert'"
        registered_id = False
        while not registered_id:
            res, err = ca_exec.execute(command.format(id=username, pw=password, type=node_type))
            if not err:
                registered_id = True
# Otherwise, unexpected error, we are having issues connecting to CA
            else:
                sleep(15)


def enroll_node(opts, ca, username, password, verbose=False):
    dir_config = opts['core']['dir_config']
    ca_namespace = get_namespace(opts, ca=ca)
    ingress_urls = ingress_read(ca + '-hlf-ca', namespace=ca_namespace, verbose=verbose)
    msp_dir = '{}_MSP'.format(username)
    msp_path = join(dir_config, msp_dir)
    if not isdir(msp_path):
        # Enroll
        command = ('FABRIC_CA_CLIENT_HOME={dir} fabric-ca-client enroll ' +
                   '-u https://{username}:{password}@{ingress} -M {msp_dir} ' +
                   '--tls.certfiles {ca_server_tls}').format(
            dir=dir_config,
            username=username,
            password=password,
            ingress=ingress_urls[0],
            msp_dir=msp_dir,
            ca_server_tls=abspath(opts['cas'][ca]['tls_cert']))
        execute_until_success(command)
    return msp_path


def create_admin(opts, msp_name, verbose=False):
    dir_config = opts['core']['dir_config']
    msp_values = opts['msps'][msp_name]
    ca_values = opts['cas'][msp_values['ca']]

    # TODO: Refactor this into its own function
    ca_name = msp_values['ca']
    ca_namespace = get_namespace(opts, ca=ca_name)

    # Get CA ingress
    ingress_urls = ingress_read(ca_name + '-hlf-ca', namespace=ca_namespace, verbose=verbose)
    ca_ingress = ingress_urls[0]

    # Register the Organisation with the CAs
    register_id(ca_namespace, msp_values['ca'], msp_values['org_admin'], msp_values['org_adminpw'], admin=True,
                verbose=verbose)

    # If our keystore does not exist or is empty, we need to enroll the identity...
    keystore = join(dir_config, msp_name, 'keystore')
    if not isdir(keystore) or not listdir(keystore):
        execute(
            ('FABRIC_CA_CLIENT_HOME={dir} fabric-ca-client enroll ' +
             '-u https://{id}:{pw}@{ingress} -M {msp_dir} --tls.certfiles {ca_server_tls}').format(
                dir=dir_config, id=msp_values['org_admin'], pw=msp_values['org_adminpw'],
                ingress=ca_ingress, msp_dir=msp_name,
                ca_server_tls=ca_values['tls_cert']
            ), verbose=verbose)


def admin_creds(opts, msp_name, verbose=False):
    msp_namespace = get_namespace(opts, msp=msp_name)
    msp_values = opts['msps'][msp_name]

    admin_cred_secret = 'hlf--{}-admincred'.format(msp_values['org_admin'])
    secret_data = credentials_secret(admin_cred_secret, msp_namespace,
                                     username=msp_values['org_admin'], password=msp_values.get('org_adminpw'),
                                     verbose=verbose)
    msp_values['org_adminpw'] = secret_data['CA_PASSWORD']


def copy_secret(from_dir, to_dir):
    from_list = glob(join(from_dir, '*'))
    if len(from_list) == 1:
        from_file = from_list[0]
    else:
        raise ValueError('from_dir contains {} files - {}'.format(
            len(from_list), from_list
        ))
    _, from_filename = split(from_file)
    to_file = join(to_dir, from_filename)
    if not isfile(to_file):
        if not isdir(to_dir):
            makedirs(to_dir)
        shutil.copy(from_file, to_file)


def msp_secrets(opts, msp_name, verbose=False):
    # Relevant variables
    msp_namespace = get_namespace(opts, msp=msp_name)
    msp_values = opts['msps'][msp_name]
    if opts['cas']:
        # If we have a CA, MSP was saved to dir_config
        msp_path = join(opts['core']['dir_config'], msp_name)
    else:
        # Otherwise we are using Cryptogen
        msp_path_list = glob('{dir_config}/crypto-config/*Organizations/{ns}*/users/Admin*/msp'.format(
            dir_config=opts['core']['dir_config'], ns=msp_namespace))
        if len(msp_path_list) == 1:
            msp_path = msp_path_list[0]
        else:
            raise ValueError('MSP path list length is {} - {}'.format(
                len(msp_path_list), msp_path_list
            ))

    # Copy cert to admincerts
    copy_secret(join(msp_path, 'signcerts'), join(msp_path, 'admincerts'))

    # Create ID secrets from Admin MSP
    id_to_secrets(msp_namespace, msp_path, msp_values['org_admin'], verbose=verbose)

    # Create CA secrets from Admin MSP
    cacerts_to_secrets(msp_namespace, msp_path, msp_values['org_admin'], verbose=verbose)


def admin_msp(opts, msp_name, verbose=False):
    admin_namespace = get_namespace(opts, msp_name)
    ns_create(admin_namespace, verbose=verbose)

    if opts['cas']:
        # Get/set credentials (if we use a CA)
        admin_creds(opts, msp_name, verbose=verbose)
        # Crypto material for Admin
        create_admin(opts, msp_name, verbose=verbose)
    else:
        print('No CAs defined in Nephos settings, ignoring Credentials')

    # Setup MSP secrets
    msp_secrets(opts, msp_name, verbose=verbose)


# General helpers
def item_to_secret(namespace, msp_path, user, item, verbose=False):
    # Item in form CryptoInfo(name, subfolder, key, required)
    secret_name = 'hlf--{user}-{type}'.format(user=user, type=item.secret_type)
    file_path = join(msp_path, item.subfolder)
    try:
        crypto_secret(secret_name,
                      namespace,
                      file_path=file_path,
                      key=item.key,
                      verbose=verbose)
    except Exception as error:
        if item.required:
            raise Exception(error)
        else:
            print('No {} found, so secret "{}" was not created'.format(file_path, secret_name))


def id_to_secrets(namespace, msp_path, user, verbose=False):
    crypto_info = [
        CryptoInfo('idcert', 'signcerts', 'cert.pem', True),
        CryptoInfo('idkey', 'keystore', 'key.pem', True)
    ]
    for item in crypto_info:
        item_to_secret(namespace, msp_path, user, item, verbose=verbose)


def cacerts_to_secrets(namespace, msp_path, user, verbose=False):
    crypto_info = [
        CryptoInfo('cacert', 'cacerts', 'cacert.pem', True),
        CryptoInfo('caintcert', 'intermediatecerts', 'intermediatecacert.pem', False)
    ]
    for item in crypto_info:
        item_to_secret(namespace, msp_path, user, item, verbose=verbose)


def setup_id(opts, msp, release, id_type, verbose=False):
    msp_values = opts['msps'][msp]
    node_namespace = get_namespace(opts, msp)
    if opts['cas']:
        ca_namespace = get_namespace(opts, ca=opts['msps'][msp]['ca'])
        # Create secret with Orderer credentials
        secret_name = 'hlf--{}-cred'.format(release)
        secret_data = credentials_secret(secret_name, node_namespace,
                                         username=release,
                                         verbose=verbose)
        # Register node
        register_id(ca_namespace, msp_values['ca'], secret_data['CA_USERNAME'], secret_data['CA_PASSWORD'], id_type,
                    verbose=verbose)
        # Enroll node
        msp_path = enroll_node(opts, msp_values['ca'],
                               secret_data['CA_USERNAME'], secret_data['CA_PASSWORD'],
                               verbose=verbose)
    else:
        # Otherwise we are using Cryptogen
        msp_path_list = glob('{dir_config}/crypto-config/{node_type}Organizations/{ns}*/{node_type}s/{node_name}*/msp'.format(
            dir_config=opts['core']['dir_config'], node_type=id_type, node_name=release, ns=node_namespace))
        if len(msp_path_list) == 1:
            msp_path = msp_path_list[0]
        else:
            raise ValueError('MSP path list length is {} - {}'.format(
                len(msp_path_list), msp_path_list
            ))
    # Secrets
    id_to_secrets(namespace=node_namespace, msp_path=msp_path, user=release, verbose=verbose)


def setup_nodes(opts, node_type, verbose=False):
    nodes = opts[node_type + 's']
    for release in nodes['names']:
        setup_id(opts, nodes['msp'], release, node_type, verbose=verbose)


# ConfigTxGen helpers
def genesis_block(opts, verbose=False):
    ord_namespace = get_namespace(opts, opts['orderers']['msp'])
    # Change to blockchain materials directory
    chdir(opts['core']['dir_config'])
    # Create the genesis block
    if not exists('genesis.block'):
        # Genesis block creation and storage
        execute(
            'configtxgen -profile OrdererGenesis -outputBlock genesis.block',
            verbose=verbose)
    else:
        print('genesis.block already exists')
    # Create the genesis block secret
    secret_from_file(secret=opts['orderers']['secret_genesis'], namespace=ord_namespace,
                     key='genesis.block', filename='genesis.block', verbose=verbose)
    # Return to original directory
    chdir(PWD)


def channel_tx(opts, verbose=False):
    peer_namespace = get_namespace(opts, opts['peers']['msp'])
    # Change to blockchain materials directory
    chdir(opts['core']['dir_config'])
    # Create Channel Tx
    channel_file = '{channel}.tx'.format(channel=opts['peers']['channel_name'])
    if not exists(channel_file):
        # Channel transaction creation and storage
        execute(
            'configtxgen -profile {channel_profile} -channelID {channel} -outputCreateChannelTx {channel_file}'.format(
                channel_profile=opts['peers']['channel_profile'],
                channel=opts['peers']['channel_name'],
                channel_file=channel_file
            ),
            verbose=verbose)
    else:
        print('{channel}.tx already exists'.format(channel=opts['peers']['channel_name']))
    # Create the channel transaction secret
    secret_from_file(secret=opts['peers']['secret_channel'], namespace=peer_namespace,
                     key=channel_file, filename=channel_file, verbose=verbose)
    # Return to original directory
    chdir(PWD)
