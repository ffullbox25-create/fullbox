from django.core.management.base import BaseCommand
from fbs.services.movement_acceptance_jobs import run_job


class Command(BaseCommand):
    help = 'Execute one durably recorded FBS movement acceptance job.'

    def add_arguments(self, parser):
        parser.add_argument('job_id', type=int)

    def handle(self, *args, **options):
        run_job(options['job_id'])
