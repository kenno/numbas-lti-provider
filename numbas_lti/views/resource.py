from .mixins import ResourceManagementViewMixin, MustBeInstructorMixin, MustHaveExamMixin, INSTRUCTOR_ROLES, lti_role_or_superuser_required
from .generic import CSVView, JSONView
from numbas_lti import forms
from numbas_lti.models import Resource, AccessToken, Exam, Attempt, ReportProcess, DiscountPart, EditorLink, COMPLETION_STATUSES, LTIUserData, ScormElement, RemarkedScormElement, AccessChange
from numbas_lti.util import transform_part_hierarchy
from django import http
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core import signing
from django.db.models import Q,Count
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.template.loader import get_template
from django.template.response import TemplateResponse
from django_auth_lti.patch_reverse import reverse
from django.utils import timezone, dateparse
from django.utils.translation import ugettext_lazy as _
from django.utils.text import slugify
from django.views import generic
from django_auth_lti.decorators import lti_role_required
from pathlib import Path
import csv
import datetime
import json
import itertools

class CreateExamView(ResourceManagementViewMixin,MustBeInstructorMixin,generic.edit.CreateView):
    model = Exam
    management_tab = 'create_exam'
    template_name = 'numbas_lti/management/create_exam.html'
    form_class = forms.CreateExamForm

    def get_context_data(self,*args,**kwargs):
        context = super(CreateExamView,self).get_context_data(*args,**kwargs)

        context['editor_links'] = EditorLink.objects.all()
        available_exams = []
        for el in EditorLink.objects.all():
            available_exams += el.available_exams
        context['exams'] = available_exams

        return context

    def form_valid(self,form):
        resource = self.request.resource
        exam = self.object = form.save(commit=False)
        exam.resource = resource
        exam.save()
        resource.exam = exam
        resource.save()
        return http.HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse('resource_settings',args=(self.request.resource.pk,))

class ReplaceExamView(CreateExamView):
    management_tab = 'settings'
    template_name = 'numbas_lti/management/replace_exam.html'
    form_class = forms.ReplaceExamForm

    def get_context_data(self,*args,**kwargs):
        resource = self.request.resource
        context = super(ReplaceExamView,self).get_context_data(*args,**kwargs)
        context['current_exam'] = resource.exam
        exams = []
        for exam in resource.exams.all():
            exams.append((exam,exam.attempts.count()))
        context['exams'] = exams
        context['num_attempts_other_versions'] = Attempt.objects.filter(resource=resource).exclude(exam=resource.exam).count()

        return context

    def form_valid(self,form):
        resource = self.request.resource
        old_exam = resource.exam
        response = super().form_valid(form)

        new_exam = self.object
        if form.cleaned_data['safe_replacement']:
            resource.attempts.filter(exam=old_exam).update(exam=new_exam)

        messages.add_message(self.request,messages.INFO,_('The exam package has been updated.'))

        return response

class RestoreExamView(MustBeInstructorMixin, ResourceManagementViewMixin,generic.edit.UpdateView):
    model = Resource
    form_class = forms.RestoreExamForm

    def get_success_url(self):
        return reverse('replace_exam',args=(self.request.resource.pk,))

class AttemptsUseCurrentVersionView(MustBeInstructorMixin, ResourceManagementViewMixin, generic.UpdateView):
    model = Resource
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        resource = self.request.resource
        with transaction.atomic():
            for a in Attempt.objects.filter(resource=resource).exclude(exam=resource.exam):
                a.exam = resource.exam
                a.save()
        messages.add_message(self.request,messages.INFO,_('All attempts now use the active version of this resource\'s exam.'))
        return redirect(reverse('replace_exam',args=(resource.pk,)))

class DashboardView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.detail.DetailView):
    model = Resource
    template_name = 'numbas_lti/management/dashboard.html'
    management_tab = 'dashboard'

    def get_exam_info(self):
        content = self.get_object().exam.source()
        if content is None:
            return
        nav = content.get('navigation',{})
        timing = content.get('timing',{})
        feedback = content.get('feedback',{})

        showResultsPage =nav.get('showresultspage')
        duration = content.get('duration')
        percentPass = content.get('percentPass')
        info = {
            'hasPercentPass': percentPass and percentPass != '0',
            'percentPass':  percentPass,
            'hasTimeLimit': duration and duration != '0',
            'duration':  int(duration)/60 if duration else None,
            'allowPrinting':  content.get('allowPrinting'),

            'allowRegen':  nav.get('allowregen'),
            'allowReverse':  nav.get('reverse'),
            'allowBrowse':  nav.get('browse'),
            'allowSteps':  nav.get('allowsteps'),

            'completionShowResultsPage': showResultsPage == 'oncompletion',
            'reviewShowResultsPage': showResultsPage == 'oncompletion' or showResultsPage == 'review',

            'leaveUnattempted': nav.get('onleave',{}).get('action') != 'preventifunattempted',
            
            'navigateMode':  nav.get('navigatemode'),
            'startPassword':  nav.get('startpassword'),

            'allowPause':  timing.get('allowPause'),

            'showActualMark':  feedback.get('showactualmark'),
            'showTotalMark':  feedback.get('showTotalMark'),
            'showAnswerState':  feedback.get('showanswerstate'),
            'allowRevealAnswer':  feedback.get('allowrevealanswer'),
            'reviewShowScore':  feedback.get('reviewshowscore'),
            'reviewShowFeedback':  feedback.get('reviewshowfeedback'),
            'reviewShowAdvice':  feedback.get('reviewshowadvice'),
            'reviewShowExpectedAnswer': feedback.get('reviewshowexpectedanswer'),
        }
        return info

    def get_context_data(self,*args,**kwargs):
        context = super(DashboardView,self).get_context_data(*args,**kwargs)

        resource = self.get_object()

        context['exam_info'] = self.get_exam_info()

        context['instructors'] = User.objects.filter(lti_data__in=LTIUserData.objects.filter(resource=resource,is_instructor=True)).distinct()

        context['num_unbroken_attempts'] = resource.attempts.exclude(broken=True).count()

        context['students'] = User.objects.filter(attempts__resource=resource).distinct()
        last_report_process = resource.report_processes.first()
        if last_report_process and last_report_process.dismissed and last_report_process.status == 'reporting':
            context['dismissed_report_process'] = last_report_process
        if last_report_process and (not last_report_process.dismissed):
            context['last_report_process'] = last_report_process

        return context

class StudentProgressView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.detail.DetailView):
    model = Resource
    template_name = 'numbas_lti/management/student_progress.html'
    management_tab = 'dashboard'

    def get_context_data(self,*args,**kwargs):
        context = super().get_context_data(*args,**kwargs)

        resource = self.get_object()

        context['unlimited_attempts'] = resource.max_attempts == 0

        context['student_summary'] = [
            (
                student,
                resource.grade_user(student),
                student.lti_data.filter(resource=resource).last(),
                Attempt.objects.filter(user=student,resource=resource).exclude(broken=True).count(),
                AccessToken.objects.filter(user=student,resource=resource).count(),
            ) 
            for student in resource.students().all()
        ]

        return context


class DiscountPartsView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.detail.DetailView):
    model = Resource
    template_name = 'numbas_lti/management/discount.html'
    context_object_name = 'resource'
    management_tab = 'dashboard'

    def get_context_data(self,*args,**kwargs):
        context = super(DiscountPartsView,self).get_context_data(*args,**kwargs)

        resource = self.get_object()

        def row(q,p,g,qnum,path,pletter,**kwargs):
            out = {
                'q': qnum,
                'p': pletter,
                'g': g,
                'path': path,
            }
            if p is not None:
                discount = DiscountPart.objects.filter(resource=resource,part=path).first()
                out.update({
                    'discount': discount,
                    'form': forms.DiscountPartBehaviourForm(instance=discount)
                })

            return out

        context['parts'] = transform_part_hierarchy(resource.part_hierarchy(),row)

        return context

class DiscountPartView(MustBeInstructorMixin,generic.base.View):

    def post(self,request,pk,*args,**kwargs):
        resource = Resource.objects.get(pk=pk)
        part = request.POST['part']
        discount,created = DiscountPart.objects.get_or_create(resource=resource,part=part)
        template = get_template('numbas_lti/management/discount/discounted.html')
        html = template.render({'resource':resource,'discount':discount,'form':forms.DiscountPartBehaviourForm(instance=discount),'path':part})
        return JsonResponse({'pk':discount.pk,'created':created, 'behaviour': discount.behaviour, 'html':html})

class DiscountPartDeleteView(MustBeInstructorMixin,generic.edit.DeleteView):
    model = DiscountPart

    def delete(self,*args,**kwargs):
        discount = self.get_object()
        discount.delete()

        resource = discount.resource
        template = get_template('numbas_lti/management/discount/not_discounted.html')
        html = template.render({'resource':resource,'path':discount.part})
        return JsonResponse({'html':html})

class DiscountPartUpdateView(MustBeInstructorMixin,generic.edit.UpdateView):
    model = DiscountPart
    form_class = forms.DiscountPartBehaviourForm

    def form_valid(self,form,*args,**kwargs):
        self.object = form.save()
        return JsonResponse({})

class ResourceSettingsView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.edit.UpdateView):
    model = Resource
    form_class = forms.ResourceSettingsForm
    template_name = 'numbas_lti/management/resource_settings.html'
    context_object_name = 'resource'
    management_tab = 'settings'

    def get_success_url(self):
        return reverse('resource_dashboard',args=(self.get_object().pk,))

class ScoresCSV(MustBeInstructorMixin,CSVView,generic.detail.DetailView):
    model = Resource
    def get_rows(self):
        headers = [_(x) for x in ['First name','Last name','Email','Username','Percentage','Raw score', 'Max score']]
        yield headers

        resource = self.object
        for student in resource.students().all():
            user_data = resource.user_data(student)
            scaled_score = resource.grade_user(student)
            max_score = max(a.max_score for a in resource.attempts.filter(user=student))
            raw_score = scaled_score * max_score    # This might introduce a rounding error
            yield (
                student.first_name,
                student.last_name,
                student.email,
                user_data.get_source_id(),
                scaled_score*100,
                raw_score,
                max_score
            )

    def get_filename(self):
        return _("{slug}-scores.csv").format(slug=self.object.slug)

class JSONDumpView(MustBeInstructorMixin,generic.detail.DetailView):
    model = Resource

    def render_to_response(self,context,**kwargs):
        full = 'full' in self.request.GET

        resource = self.get_object()
        head = '''{{
    "resource": {{
        "pk": {pk},
        "title": {title}
    }},
    "attempts": ['''.format(pk=resource.pk,title=json.dumps(resource.title))
        footer = '    ]\n}'

        response = http.StreamingHttpResponse(
            itertools.chain([head],((',' if i>0 else '')+json.dumps(a.data_dump(include_all_scorm=full)) for i,a in enumerate(resource.attempts.all())),[footer]),
            content_type='application/json'
        )
        response['Content-Disposition'] = 'attachment; filename="{context}--{resource}.json"'.format(context=slugify(resource.context.name), resource=resource.slug)
        return response


class AttemptsCSV(MustBeInstructorMixin,CSVView,generic.detail.DetailView):
    model = Resource
    def get_rows(self):
        resource = self.object
        num_questions = resource.num_questions

        headers = [_(x) for x in ['First name','Last name','Email','Username','Start time','End time','Completed?','Total score','Percentage']]+[_('Question {n}').format(n=i+1) for i in range(num_questions)]
        yield headers

        for attempt in resource.attempts.all():
            user_data = resource.user_data(attempt.user)
            row = [
                attempt.user.first_name,
                attempt.user.last_name,
                attempt.user.email,
                user_data.get_source_id(),
                attempt.start_time,
                attempt.end_time,
                attempt.completion_status,
                attempt.raw_score,
                attempt.scaled_score*100,
            ]+[attempt.question_raw_score(n) for n in range(num_questions)]
            yield row

    def get_filename(self):
        return _("{slug}-attempts.csv").format(slug=self.object.slug)

class ReportAllScoresView(MustHaveExamMixin,MustBeInstructorMixin,ResourceManagementViewMixin,generic.detail.DetailView):
    model = Resource
    management_tab = 'dashboard'
    template_name = 'numbas_lti/management/report_all_scores.html'
    context_object_name = 'resource'

    def get(self,*args,**kwargs):
        resource = self.get_object()
        resource.task_report_scores()
        return super(ReportAllScoresView,self).get(*args,**kwargs)

@lti_role_or_superuser_required(INSTRUCTOR_ROLES)
def grant_access_token(request,resource_id,user_id):
    resource = Resource.objects.get(pk=resource_id)
    user = User.objects.get(id=user_id)
    AccessToken.objects.create(user=user,resource=resource)

    return redirect(reverse('resource_dashboard',args=(resource.pk,)))

@lti_role_or_superuser_required(INSTRUCTOR_ROLES)
def remove_access_token(request,resource_id,user_id):
    resource = Resource.objects.get(pk=resource_id)
    user = User.objects.get(id=user_id)
    AccessToken.objects.filter(user=user,resource=resource).first().delete()

    return redirect(reverse('resource_dashboard',args=(resource.pk,)))

class DismissReportProcessView(MustBeInstructorMixin,generic.detail.DetailView):
    model = ReportProcess

    def render_to_response(self,context):
        process = self.get_object()
        process.dismissed = True
        process.save()
        return redirect(reverse('resource_dashboard',args=(process.resource.pk,)))

class RunExamView(MustHaveExamMixin,MustBeInstructorMixin,ResourceManagementViewMixin,generic.detail.DetailView):
    """
        Run an exam without saving any attempt data
    """
    management_tab = 'test_run'
    model = Exam
    template_name = 'numbas_lti/management/run_exam.html'
    context_object_name = 'exam'

    def get_resource(self):
        return Resource.objects.filter(exam=self.get_object()).first()

    def get_context_data(self,*args,**kwargs):
        context = super(RunExamView,self).get_context_data(*args,**kwargs)

        context['scorm_cmi'] = json.dumps({
            'cmi.mode': 'normal',
            'cmi.entry': 'ab-initio',
            'cmi.suspend_data': '',
            'cmi.objectives._count': 0,
            'cmi.interactions._count': 0,
            'cmi.learner_name': self.request.user.get_full_name(),
            'cmi.learner_id': self.request.user.username,
            'cmi.location': '',
            'cmi.score.raw': 0,
            'cmi.score.scaled': 0,
            'cmi.score.min': 0,
            'cmi.score.max': 0,
            'cmi.total_time': 0,
            'cmi.completion_status': '',
            'cmi.success_status': ''
        })
        
        return context

class AllAttemptsView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.ListView):
    model = Attempt
    template_name = 'numbas_lti/management/attempts.html'
    management_tab = 'attempts'
    paginate_by = 20
    context_object_name = 'attempts'

    def get_queryset(self, *args, **kwargs):
        self.query = ''
        resource = self.get_resource()
        attempts = resource.attempts.all()
        if self.request.GET.get('userid'):
            try:
                user = User.objects.get(pk=int(self.request.GET['userid']))
                self.query = user.get_full_name()
                attempts = attempts.filter(user=user)
            except (ValueError, User.DoesNotExist):
                pass
        elif 'query' in self.request.GET:
            query = self.query = self.request.GET.get('query')
            for word in query.split():
                attempts = attempts.filter(Q(user__first_name__icontains=word) | Q(user__last_name__icontains=word))
        return attempts

    def get_resource(self):
        return Resource.objects.get(pk=self.kwargs.get('pk'))

    def get_context_data(self, *args, **kwargs):
        context = super(AllAttemptsView,self).get_context_data(*args,**kwargs)
        resource = self.get_resource()
        context['resource'] = resource
        context['query'] = self.query

        return context

class StatsView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.DetailView):
    model = Resource
    template_name = 'numbas_lti/management/resource_stats.html'
    management_tab = 'stats'

    def get_context_data(self, *args, **kwargs):
        context = super(StatsView,self).get_context_data(*args, **kwargs)

        resource = self.object

        completion_counts = resource.attempts.values('completion_status').order_by('completion_status').annotate(number=Count('completion_status'))
        completion_dict = {x['completion_status']: x['number'] for x in completion_counts}
        context['completion_counts'] = [
            (label, value, completion_dict.get(value,0)) for value,label in COMPLETION_STATUSES
        ]

        context['data'] = resource.live_stats_data()

        return context

class RemarkView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.DetailView):
    model = Resource
    template_name = 'numbas_lti/management/resource_remark.html'
    management_tab = 'remark'

    def get(self, request, *args, **kwargs):
        resource = self.object = self.get_object()
        if not resource.exam.supports_feature('run_headless'):
            return TemplateResponse(
                request=self.request,
                template=['numbas_lti/management/resource_remark_not_supported.html'],
                context=super().get_context_data(object=self.object),
                using=self.template_engine
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args,**kwargs)

        resource = self.object
        attempts = resource.unbroken_attempts()

        context['attempts'] = [
            {
                'pk': a.pk,
                'completion_status': a.completion_status,
                'user': { 
                    'full_name': a.user.get_full_name(),
                    'identifier': a.user_data().identifier(),
                },
            }
            for a in attempts
        ]

        context['parameters'] = {
            'save_url': reverse('resource_remark_save_data',args=(resource.pk,)),
        }

        source_path = Path(resource.exam.extracted_path) / 'source.exam'
        if source_path.exists():
            with open(str(source_path)) as f:
                text = f.read()
                i = text.find('\n')
                data = json.loads(text[i+1:])
                context['exam_source'] = data

        return context

class RemarkGetAttemptDataView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.DetailView):
    """ 
        Get the SCORM CMI for the given attempts 
    """
    model = Resource

    def get(self,request,*args,**kwargs):
        pks = request.GET.get('attempt_pks','')
        if pks:
            pks = [int(x) for x in pks.split(',')]
        else:
            pks = []
        attempts = self.resource.attempts.filter(pk__in=pks)

        cmis = []
        for a in attempts:
            cmi = a.scorm_cmi()

            dynamic_cmi = {
                'cmi.mode': 'review',
                'cmi.entry': 'resume',
                'numbas.user_role': 'student',
            }
            etime = datetime.datetime.now().timestamp()
            dynamic_cmi = {k: {'value':v,'time':etime} for k,v in dynamic_cmi.items()}
            cmi.update(dynamic_cmi)
            cmis.append({
                'pk': a.pk, 
                'cmi': cmi
                })

        return JsonResponse({'cmis': cmis})

class RemarkSaveChangedDataView(MustHaveExamMixin, ResourceManagementViewMixin, MustBeInstructorMixin, generic.UpdateView):
    """
        Save changed SCORM elements after remarking
    """
    model = Resource

    def post(self, request, *args, **kwargs):
        saved = []
        try:
            data = json.loads(request.body.decode())
            now = timezone.make_aware(datetime.datetime.now())
            with transaction.atomic():
                for ad in data['attempts']:
                    try:
                        attempt = Attempt.objects.get(pk=ad['pk'])
                    except Attempt.DoesNotExist:
                        continue

                    for k,v in ad['changed_keys'].items():
                        e = ScormElement.objects.create(attempt=attempt, key=k, value=v, time=now, counter=0)
                        RemarkedScormElement.objects.create(element=e,user=request.user)
                    saved.append(ad['pk'])
            response = {'success': True, 'saved': saved}
            if len(saved)<len(data['attempts']):
                response['success'] = False
                response['message'] = _("There was an error while saving some data.")
            return JsonResponse(response, status=200 if response['success'] else 500)
        except Exception as err:
            return JsonResponse({'success': False, 'message': str(err), 'saved': saved},status=500)

class RemarkIframeView(MustHaveExamMixin,ResourceManagementViewMixin,MustBeInstructorMixin,generic.DetailView):
    model = Resource
    template_name = 'numbas_lti/management/resource_remark_iframe.html'
    management_tab = 'remark'

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args,**kwargs)
        resource = self.object
        context['scripts_url'] = resource.exam.extracted_url +'/scripts.js'
        return context

class ValidateReceiptView(ResourceManagementViewMixin,MustBeInstructorMixin,generic.detail.SingleObjectMixin,generic.FormView):
    model = Resource
    form_class = forms.ValidateReceiptForm
    template_name = 'numbas_lti/management/validate_receipt.html'
    management_tab = 'dashboard'
    
    def dispatch(self,request,*args,**kwargs):
        self.object = self.get_object()
        return super().dispatch(request,*args,**kwargs)

    def get(self,request,*args,**kwargs):
        return super().get(request,*args,**kwargs)

    def form_valid(self, form):
        code = form.cleaned_data['code']
        salt = self.object.receipt_salt()
        context = self.get_context_data()
        context['form'] = form
        context['submitted'] = True
        try:
            summary = signing.loads(code,salt=salt)
            for k in ('receipt_time','start_time','end_time'):
                if k in summary and summary[k] is not None:
                    summary[k] = dateparse.parse_datetime(summary[k])
            context['summary'] = summary
            attempt = Attempt.objects.get(pk=summary['pk'],resource=self.object)
            context['attempt'] = attempt
            context['valid'] = True
            return self.render_to_response(context)
        except signing.BadSignature:
            context['invalid'] = True
            pass
        except Attempt.DoesNotExist:
            context['no_attempt'] = True

        return self.render_to_response(context)

class AccessChangesView(ResourceManagementViewMixin, MustBeInstructorMixin, generic.ListView):
    model = AccessChange
    template_name = 'numbas_lti/management/access_change/list.html'
    management_tab = 'access-changes'
    resource_pk_url_kwarg = 'resource_id'
    
class CreateAccessChangeView(ResourceManagementViewMixin, MustBeInstructorMixin, generic.CreateView):
    model = AccessChange
    form_class = forms.AccessChangeForm
    template_name = 'numbas_lti/management/access_change/edit.html'
    management_tab = 'access-changes'
    resource_pk_url_kwarg = 'resource_id'

    extra_context = {
        'create': True,
    }

    def get_initial(self):
        initial = super().get_initial()
        initial['resource'] = self.get_resource()

        return initial

    def get_success_url(self):
        return reverse('resource_access_changes',args=(self.get_resource().pk,))

class UpdateAccessChangeView(ResourceManagementViewMixin, MustBeInstructorMixin, generic.UpdateView):
    model = AccessChange
    form_class = forms.AccessChangeForm
    management_tab = 'access-changes'
    template_name = 'numbas_lti/management/access_change/edit.html'

    def get_resource(self):
        return self.get_object().resource

    def get_success_url(self):
        return reverse('resource_access_changes',args=(self.get_resource().pk,))

    def get_initial(self):
        initial = super().get_initial()
        ac = self.get_object()
        initial['resource'] = ac.resource
        initial['usernames'] = '\n'.join(u.username for u in ac.usernames.all())
        initial['emails'] = '\n'.join(e.email for e in ac.emails.all())
        if ac.extend_deadline is not None:
            initial['extend_deadline_days'] = ac.extend_deadline.days
            initial['extend_deadline_minutes'] = ac.extend_deadline.seconds // 60
        return initial

class DeleteAccessChangeView(ResourceManagementViewMixin, MustBeInstructorMixin, generic.DeleteView):
    model = AccessChange
    management_tab = 'settings'
    template_name = 'numbas_lti/management/access_change/delete.html'

    def get_resource(self):
        return self.get_object().resource

    def get_success_url(self):
        return reverse('resource_access_changes',args=(self.get_resource().pk,))
