# Brand website URL for the product-questions crawl; answer source on the log.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0066_ba_product_qa'),
    ]

    operations = [
        migrations.AddField(
            model_name='baproductquestion',
            name='source',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='tenant',
            name='brand_website_url',
            field=models.CharField(blank=True, default='', max_length=300),
        ),
    ]
