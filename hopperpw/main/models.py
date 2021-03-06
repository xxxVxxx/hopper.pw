# coding=utf-8
import base64
import binascii
from datetime import datetime
import dns.resolver
import re

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.signals import post_delete

from main import dnstools


class BaseModel(models.Model):
    last_update = models.DateTimeField(auto_now=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class BaseModelCreatedBy(BaseModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True, null=True)

    class Meta:
        abstract = True


class BaseModelRequiredCreatedBy(BaseModel):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL)

    class Meta:
        abstract = True


class BlacklistedDomain(BaseModelCreatedBy):
    domain = models.CharField(
        max_length=256,
        unique=True,
        help_text='Blacklisted domain. Evaluated as regex (search).')

    def __unicode__(self):
        return u"%s" % (self.domain, )


def domain_blacklist_validator(value):
    for bd in BlacklistedDomain.objects.all():
        if re.search(bd.domain, value):
            raise ValidationError(u'This domain is not allowed')


def ns_update_key_validator(value):
    try:
        base64.decodestring(value)
    except binascii.Error as e:
        raise ValidationError(u'Invalid update key: Must be base64 (%s)' % e)


UPDATE_ALGORITHMS = (
    ('HMAC_SHA512', 'HMAC_SHA512'),
)


class Domain(BaseModelCreatedBy):
    domain = models.CharField(max_length=256, unique=True)
    nameserver_ip = models.IPAddressField(
        max_length=256)
    nameserver_update_key = models.CharField(max_length=256, validators=[
        ns_update_key_validator])
    nameserver_update_algorithm = models.CharField(
        max_length=256, choices=UPDATE_ALGORITHMS)
    available_for_everyone = models.BooleanField(default=False)

    def __unicode__(self):
        return u"%s" % (self.domain, )


class Host(BaseModelRequiredCreatedBy):
    subdomain = models.CharField(max_length=256, validators=[
        RegexValidator(
            regex=r'^(([a-z0-9][a-z0-9\-]*[a-z0-9])|[a-z0-9])$',
            message='Invalid subdomain: only "a-z", "0-9" and "-" is allowed'
        ),
        domain_blacklist_validator])
    domain = models.ForeignKey(Domain)
    update_secret = models.CharField(max_length=256)
    comment = models.CharField(
        max_length=256, default='', blank=True, null=True)

    last_api_update = models.DateTimeField(blank=True, null=True)

    def __unicode__(self):
        return u"%s.%s - %s" % (
            self.subdomain, self.domain.domain, self.comment)

    class Meta:
        unique_together = (('subdomain', 'domain'),)

    def get_fqdn(self):
        return '%s.%s' % (self.subdomain, self.domain.domain)

    @classmethod
    def filter_by_fqdn(cls, fqdn, **kwargs):
        # Assuming subdomain has no dots (.) the fqdn is split at the first dot
        if not '.' in fqdn:
            return Host.objects.none()
        splitted = fqdn.split('.', 1)
        return Host.objects.filter(
            subdomain=splitted[0], domain__domain=splitted[1], **kwargs)

    def getIPv4(self):
        try:
            return dnstools.query_ns(self.get_fqdn(), 'A', origin=self.domain.domain)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
            return ''

    def getIPv6(self):
        try:
            return dnstools.query_ns(self.get_fqdn(), 'AAAA', origin=self.domain.domain)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.resolver.Timeout):
            return ''

    def poke(self):
        self.last_api_update = datetime.now()
        self.save()

    def generate_secret(self):
        # note: we use a quick hasher for the update_secret as expensive
        # more modern hashes might put too much load on the servers. also
        # many update clients might use http without ssl, so it is not too
        # secure anyway.
        secret = User.objects.make_random_password()
        self.update_secret = make_password(
            secret,
            hasher='sha1'
        )
        self.save()
        return secret


def post_delete_host(sender, **kwargs):
    obj = kwargs['instance']
    dnstools.delete(obj.get_fqdn(), origin=obj.domain.domain)


post_delete.connect(post_delete_host, sender=Host)

