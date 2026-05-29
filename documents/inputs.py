import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class AddDocumentInput(SparkGraphQLInput):
    """Register an already-uploaded GCS blob as a BA document.

    `file` is the GCS blob PATH (blobName) returned by getUploadUrl, NOT
    a signed URL. Same contract as recaps addRecapFile.
    """
    doc_type: str
    file: str
    title: str | None = None
    expires_on: str | None = None  # "YYYY-MM-DD"
    original_filename: str | None = None
    content_type: str | None = None


@strawberry.input
class DeleteDocumentInput(SparkGraphQLInput):
    uuid: strawberry.ID
