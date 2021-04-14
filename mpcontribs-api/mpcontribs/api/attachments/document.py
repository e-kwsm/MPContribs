# -*- coding: utf-8 -*-
import json
import boto3
import binascii

from hashlib import md5
from flask import request
from base64 import b64decode, b64encode
from flask_mongoengine import DynamicDocument
from mongoengine import signals
from mongoengine.fields import StringField
from mongoengine import ValidationError
from filetype.types.archive import Gz
from filetype.types.image import Jpeg, Png, Gif, Tiff

from mpcontribs.api.config import API_CNAME

MAX_BYTES = 200 * 1024
BUCKET = "mpcontribs-attachments"
S3_ATTACHMENTS_URL = f"https://{BUCKET}.s3.amazonaws.com"
SUPPORTED_FILETYPES = (Gz, Jpeg, Png, Gif, Tiff)
SUPPORTED_MIMES = [t().mime for t in SUPPORTED_FILETYPES]

s3_client = boto3.client("s3")


def get_key(md5):
    return f"{API_CNAME}/{md5}"


class Attachments(DynamicDocument):
    name = StringField(required=True, help_text="file name")
    md5 = StringField(regex=r"^[a-z0-9]{32}$", unique=True, help_text="md5 sum")
    mime = StringField(required=True, choices=SUPPORTED_MIMES, help_text="attachment mime type")
    content = StringField(required=True, help_text="base64-encoded attachment content")
    meta = {"collection": "attachments", "indexes": ["name", "mime", "md5"]}

    @classmethod
    def post_init(cls, sender, document, **kwargs):
        if document.id and document._data.get("content"):
            from mpcontribs.api.attachments.views import AttachmentsResource
            res = AttachmentsResource()
            requested_fields = res.get_requested_fields(params=request.args)

            if "content" in requested_fields:
                if not document.md5:
                    # document.reload("md5")  # TODO AttributeError: _changed_fields
                    raise ValueError("Please also request md5 field to retrieve attachment content!")

                retr = s3_client.get_object(Bucket=BUCKET, Key=get_key(document.md5))
                document.content = b64encode(retr["Body"].read()).decode("utf-8")

    @classmethod
    def pre_delete(cls, sender, document, **kwargs):
        s3_client.delete_object(Bucket=BUCKET, Key=get_key(document.md5))

    @classmethod
    def pre_save_post_validation(cls, sender, document, **kwargs):
        from mpcontribs.api.attachments.views import AttachmentsResource

        # b64 decode
        try:
            content = b64decode(document.content, validate=True)
        except binascii.Error:
            raise ValidationError(f"Attachment {document.name} not base64 encoded!")

        # check size
        size = len(content)

        if size > MAX_BYTES:
            raise ValidationError(
                f"Attachment {document.name} too large ({size} > {MAX_BYTES})!"
            )

        # md5
        resource = AttachmentsResource()
        d = resource.serialize(document, fields=["mime", "content"])
        s = json.dumps(d, sort_keys=True).encode("utf-8")
        document.md5 = md5(s).hexdigest()

        # save to S3 and unset content
        s3_client.put_object(
            Bucket=BUCKET,
            Key=get_key(document.md5),
            ContentType=document.mime,
            Metadata={"name": document.name},
            Body=content,
        )
        document.content = size  # set to something useful to distinguish in post_init


signals.post_init.connect(Attachments.post_init, sender=Attachments)
signals.pre_delete.connect(Attachments.pre_delete, sender=Attachments)
signals.pre_save_post_validation.connect(Attachments.pre_save_post_validation, sender=Attachments)