import streamlit as st
from collections import defaultdict
from dotenv import load_dotenv
from UI.sidebar import Sidebar
from utility.s3 import S3
from utility.sessionstate import Init
from UI.main import Main


def get_user_collection_name(full_name):
    user_collection_name = full_name.split('-', 1)[1] if '-' in full_name else full_name
    return user_collection_name

def on_selectbox_change():
    st.session_state.show_info = True


def main():
    load_dotenv()
    st.set_page_config(page_title="DB MANAGEMENT", page_icon="🗃️", layout="wide")
    st.title("Database Management 🗃️")

    with st.empty():
        Init.initialize_session_state()
        Init.initialize_clientdb_state()
        

    existing_collections = st.session_state.client_db.get_existing_collections()
    if not existing_collections:  # No existing collections
        st.warning("There are no existing collections. Please create a new collection to get started.")
        new_collection_name = st.text_input("Enter the name of the new collection:")
        if st.button("Create New Collection"):
            if new_collection_name:
                st.session_state.client_db.client.create_collection(new_collection_name)
                st.success(f"Collection {new_collection_name} created successfully!")
                st.experimental_rerun()  # Rerun the script to refresh the state and UI
            else:
                st.error("Please enter a valid name for the new collection.")
    else:
        selected_collection_name, selected_collection_object = Main.handle_collection_selection(existing_collections)

        with st.sidebar:
            Sidebar.file_upload_and_ingest(st.session_state.client_db, selected_collection_name, selected_collection_object, on_selectbox_change)

        collection_tab, settings_tab = st.tabs(["Collection", "Settings"])

        with collection_tab:

            if selected_collection_object:
                document_count = selected_collection_object.count()
                st.subheader(f"There are {document_count} pages in the selected_collection_object.")
                
                def toggle_display_ui():
                    st.session_state.display_ui_clicked = not st.session_state.get('display_ui_clicked', False)

                display_ui_checkbox = st.checkbox(
                    "Display UI", 
                    value=st.session_state.get('display_ui_clicked', False),
                    on_change=toggle_display_ui
                )

                if display_ui_checkbox:
                    if document_count > 0:
                        # Listing the first 10 documents
                        documents = selected_collection_object.peek(limit=document_count)

                        # Displaying each document chunk with actions
                        ids = documents['ids']
                        contents = documents.get('documents', [])
                        metadatas = documents.get('metadatas', [])

                        parent_docs_dict = defaultdict(list)
                        for doc_id, doc in zip(documents['ids'], documents['documents']):
                            # Extracting the file name from the doc_id
                            file_name = doc_id.rsplit('_', 1)[0]  # Split by the last underscore
                            parent_docs_dict[file_name].append(doc)
                        
                        # Create UI Elements to Select Parent Document
                        parent_docs_sorted = sorted(parent_docs_dict.keys())
                        st.subheader(f"There are {len(parent_docs_sorted)} documents in the selected_collection_object.")
                        parent_doc = st.radio('Select a document:', parent_docs_sorted)

                        filtered_ids = [doc_id for doc_id in ids if doc_id.startswith(parent_doc)]
                        #st.write(filtered_ids)

                        filtered_docs = [(doc_id, metadatas[ids.index(doc_id)]) for doc_id in filtered_ids]
                    

                        if st.button(f"Delete '{parent_doc}'"):
                            st.session_state['delete'] = True

                        if st.session_state.get('delete', False):
                            st.warning(f"Are you sure you want to delete all chunks in {parent_doc}? This action cannot be undone.")

                            if st.button("Yes, Delete All"):
                                s3_urls_to_delete = [metadata.get('file_url') for metadata in metadatas if metadata.get('unique_id') in filtered_ids]
                                st.write(s3_urls_to_delete)
            
                                # Delete files from S3
                                if s3_urls_to_delete:
                                    s3_instance = S3()  # Create an instance of the S3 class
                                    s3_instance.delete_document_objects(s3_urls_to_delete)

                                st.write("Deleting the following IDs: ", filtered_ids)  # Displaying IDs being deleted
                                selected_collection_object.delete(ids=filtered_ids)
                                st.session_state['deleted'] = True
                    
                                # Reset 'delete' state to False
                                st.session_state['delete'] = False

                                st.experimental_rerun()

                        if st.session_state.get('deleted', False):

                            st.success(f"The selected document deleted successfully!")
                            
                            st.session_state['deleted'] = False



                        # Creating columns
                        col1, col2 = st.columns(2)
                        
                        # First Column

                        with col1:
                            st.subheader("Available IDs")
                            combined_ids = [f"(Page {doc_metadata['page_number']}): {doc_id}" for doc_id, doc_metadata in filtered_docs]
                            selected_chunk_id_combined = st.selectbox('Select a chunk:', combined_ids)
                            selected_chunk_id = selected_chunk_id_combined.split(": ", 1)[1]

                            with st.expander("View All IDs"):
                                for combined_id in combined_ids:
                                    st.write(combined_id)
                            
                        # Find the selected document
                        idx = ids.index(selected_chunk_id)  # Find the index of selected_chunk_id in ids list
                        selected_content = contents[idx] if idx < len(contents) else ""
                        selected_metadata = metadatas[idx] if idx < len(metadatas) else ""
                        
                        # Display content
                        with col2:
                            st.subheader("Content:")
                            st.write(selected_content)

                            # Display metadata and content for the selected chunk
                            st.subheader("Metadata")
                            if isinstance(selected_metadata, dict):
                                for key, value in selected_metadata.items():
                                    st.write(f"{key}: {value}")
                            else:
                                st.write(selected_metadata)
                            
                            
                            if st.button(f"Delete Page"):
                                st.session_state['delete_chunk'] = True

                            if st.session_state.get('delete_chunk', False):
                                st.warning(f"Are you sure you want to delete {selected_chunk_id}? This action cannot be undone.")

                                if st.button("Yes, Delete"):
                                    selected_collection_object.delete(selected_chunk_id)
                                    st.session_state['deleted_chunk'] = True
                                    
                                    # Reset 'delete_chunk' state to False
                                    st.session_state['delete_chunk'] = False

                                    st.experimental_rerun()

                            if st.session_state.get('deleted_chunk', False):
                                st.success(f"{selected_chunk_id} deleted successfully!")
                                
                                # Reset 'deleted_chunk' state
                                st.session_state['deleted_chunk'] = False
            else:
                st.error("Please select a collection to continue.")

        with settings_tab:
            st.subheader("Settings 🛠️")

            with st.expander("List of Collections"):
                st.write(existing_collections)
            
            # Creating New Collection
            with st.expander("Create New Collection"):
                Main.create_new_collection()

            # Deleting a Collection
            with st.expander("Delete a Collection"):
                Main.delete_collection(existing_collections)

            with st.expander("Rename a Collection"):
                Main.rename_collection(existing_collections)



if __name__ == "__main__":
    main()