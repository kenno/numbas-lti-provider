# -*- coding: utf-8 -*-
# Generated by Django 1.9.7 on 2016-11-01 13:35
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('numbas_lti', '0022_discountpart'),
    ]

    operations = [
        migrations.AddField(
            model_name='attempt',
            name='broken',
            field=models.BooleanField(default=False),
        ),
    ]
