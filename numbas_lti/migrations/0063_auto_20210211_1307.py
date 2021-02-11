# Generated by Django 2.2.13 on 2021-02-11 13:07

from django.db import migrations, models
import django.db.models.deletion

def set_exam_resources(apps, schema_editor):
    Resource = apps.get_model('numbas_lti', 'Resource')
    Attempt = apps.get_model('numbas_lti', 'Attempt')

    for r in Resource.objects.exclude(exam=None):
        r.exam.resource = r
        r.exam.save()

    for a in Attempt.objects.exclude(exam=None):
        if a.exam.resource is None:
            a.exam.resource = a.resource
            a.exam.save()

class Migration(migrations.Migration):

    dependencies = [
        ('numbas_lti', '0062_scormelementdiff'),
    ]

    operations = [
        migrations.AddField(
            model_name='exam',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='exams', to='numbas_lti.Resource'),
        ),
        migrations.AlterField(
            model_name='resource',
            name='exam',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='main_exam_of', to='numbas_lti.Exam'),
        ),
        migrations.RunPython(set_exam_resources,migrations.RunPython.noop),
    ]
